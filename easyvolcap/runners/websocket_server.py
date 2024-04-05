"""
Create a websocket server during initialization and send the rendered images to the client
"""
from __future__ import annotations

import os
import glm
import time
import torch
import asyncio
import threading
import turbojpeg
import websockets
import numpy as np
import torch.nn.functional as F

from copy import deepcopy
from typing import List, Union, Dict
from glm import vec3, vec4, mat3, mat4, mat4x3
# from torchvision.io import decode_jpeg, encode_jpeg


from easyvolcap.engine import cfg  # need this for initialization?
from easyvolcap.engine import RUNNERS  # controls the optimization loop of a particular epoch
from easyvolcap.runners.volumetric_video_runner import VolumetricVideoRunner
from easyvolcap.runners.volumetric_video_viewer import VolumetricVideoViewer

from easyvolcap.utils.console_utils import *
from easyvolcap.utils.viewer_utils import Camera
from easyvolcap.utils.timer_utils import timer
from easyvolcap.utils.data_utils import add_iter, add_batch, to_cuda, Visualization


@RUNNERS.register_module()
class WebSocketServer(VolumetricVideoViewer):
    # Viewer should be used in conjuction with another runner, which explicitly handles model loading
    def __init__(self,
                 # Runner related parameter & config
                 runner: VolumetricVideoRunner,  # already built outside of this init

                 # Socket related initialization
                 host: str = '0.0.0.0',
                 send_port: int = 1024,
                 recv_port: int = 1025,

                 # Camera related config
                 camera_cfg: dotdict = dotdict(H=1080, W=1920),
                 jpeg_quality: int = 75,

                 **kwargs,
                 ):

        # Socket related initialization
        self.host = host
        self.send_port = send_port
        self.recv_port = recv_port

        # Initialize server-side camera in case there's lag
        self.camera_cfg = camera_cfg
        self.camera = Camera(**camera_cfg)
        self.camera_lock = threading.Lock()
        self.image_lock = threading.Lock()
        self.image = torch.randint(0, 255, (self.H, self.W, 4), dtype=torch.uint8)
        self.stream = torch.cuda.Stream()
        self.jpeg = turbojpeg.TurboJPEG()
        self.jpeg_quality = jpeg_quality
        self.exposure = 1.0
        self.offset = 0.0

        # Runner initialization
        self.runner = runner
        self.runner.visualizer.store_alpha_channel = True  # disable alpha channel for rendering on viewer
        self.runner.visualizer.uncrop_output_images = False  # manual uncropping
        self.visualization_type = Visualization.RENDER
        self.epoch = self.runner.load_network()  # load weights only (without optimizer states)
        self.dataset = self.runner.val_dataloader.dataset
        self.model = self.runner.model
        self.model.eval()

    def run(self):
        def start_send_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            log('Preparing send server')
            send_server = websockets.serve(self.send_loop, self.host, self.send_port)

            loop.run_until_complete(send_server)
            loop.run_forever()

        def start_recv_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            log('Preparing recv server')
            recv_server = websockets.serve(self.recv_loop, self.host, self.recv_port)

            loop.run_until_complete(recv_server)
            loop.run_forever()

        self.send_thread = threading.Thread(target=start_send_server, daemon=True)
        self.recv_thread = threading.Thread(target=start_recv_server, daemon=True)
        self.send_thread.start()
        self.recv_thread.start()
        self.render_loop()  # the rendering runs on the main thread

    def render_loop(self):  # this is the main thread
        frame_cnt = 0
        prev_time = time.perf_counter()

        while True:
            with self.camera_lock:
                batch = self.camera.to_batch()  # fast copy of camera parameter
            image = self.render(batch)  # H, W, 4, cuda gpu tensor
            self.stream.wait_stream(torch.cuda.current_stream())  # initiate copy after main stream has finished
            with torch.cuda.stream(self.stream):
                with self.image_lock:
                    self.image = image.to('cpu', non_blocking=True)  # initiate async copy

            curr_time = time.perf_counter()
            pass_time = curr_time - prev_time
            frame_cnt += 1
            if pass_time > 2.0:
                fps = frame_cnt / pass_time
                frame_cnt = 0
                prev_time = curr_time
                log(f'Render FPS: {fps}')

    async def send_loop(self, websocket: websockets.WebSocket, path: str):
        frame_cnt = 0
        prev_time = time.perf_counter()

        while True:
            with self.image_lock:
                self.stream.synchronize()  # waiting for the copy event to complete
                image = self.image.numpy()  # copy to new memory space
            image = self.jpeg.encode(image, self.jpeg_quality, pixel_format=turbojpeg.TJPF_RGBA)
            websocket.send(image)

            curr_time = time.perf_counter()
            pass_time = curr_time - prev_time
            frame_cnt += 1
            if pass_time > 2.0:
                fps = frame_cnt / pass_time
                frame_cnt = 0
                prev_time = curr_time
                log(f'Send FPS: {fps}')

    async def recv_loop(self, websocket: websockets.WebSocket, path: str):
        while True:
            time.sleep(1)

    def render(self, batch: dotdict):
        batch = to_cuda(add_iter(add_batch(batch), 0, 1))  # int -> tensor -> add batch -> cuda, smalle operations are much faster on cpu

        # Forward pass
        self.runner.maybe_jit_model(batch)
        with torch.inference_mode(self.runner.test_using_inference_mode), torch.no_grad(), torch.cuda.amp.autocast(enabled=self.runner.test_use_amp, cache_enabled=self.runner.test_amp_cached):
            output = self.model(batch)

        image = self.runner.visualizer.generate_type(output, batch, self.visualization_type)[0][0]  # RGBA (should we use alpha?)

        if self.exposure != 1.0 or self.offset != 0.0:
            image = torch.cat([(image[..., :3] * self.exposure + self.offset), image[..., -1:]], dim=-1)  # add manual correction
        image = (image.clip(0, 1) * 255).type(torch.uint8).flip(0)  # transform

        return image  # H, W, 4
