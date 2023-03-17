import pdb
import shutil
from share import *

import os
import numpy as np
import torch
from PIL import Image

from tqdm import trange
from pytorch_lightning import seed_everything
from annotator.util import resize_image, HWC3
from annotator.openpose import OpenposeDetector
from annotator.canny import CannyDetector
from annotator.uniformer import UniformerDetector
from cldm.model import create_model, load_state_dict
from cldm.ddim_hacked import DDIMSampler
from controlnet.annotator.openpose import util

def extract_into_tensor(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))

@torch.no_grad()
def interpolate_spherical(p0, p1, fract_mixing: float):
    r""" Copied from lunarring/latentblending
    Helper function to correctly mix two random variables using spherical interpolation.
    The function will always cast up to float64 for sake of extra 4.
    Args:
        p0: 
            First tensor for interpolation
        p1: 
            Second tensor for interpolation
        fract_mixing: float 
            Mixing coefficient of interval [0, 1]. 
            0 will return in p0
            1 will return in p1
            0.x will return a mix between both preserving angular velocity.
    """ 
    
    if p0.dtype == torch.float16:
        recast_to = 'fp16'
    else:
        recast_to = 'fp32'
    
    p0 = p0.double()
    p1 = p1.double()
    norm = torch.linalg.norm(p0) * torch.linalg.norm(p1)
    epsilon = 1e-7
    dot = torch.sum(p0 * p1) / norm
    dot = dot.clamp(-1+epsilon, 1-epsilon)
    
    theta_0 = torch.arccos(dot)
    sin_theta_0 = torch.sin(theta_0)
    theta_t = theta_0 * fract_mixing
    s0 = torch.sin(theta_0 - theta_t) / sin_theta_0
    s1 = torch.sin(theta_t) / sin_theta_0
    interp = p0*s0 + p1*s1
    
    if recast_to == 'fp16':
        interp = interp.half()
    elif recast_to == 'fp32':
        interp = interp.float()
        
    return interp

def to_pil(rgb):
    if len(rgb.shape) == 3:
        return Image.fromarray((rgb*255).detach().cpu().numpy().astype('uint8'))
    return Image.fromarray((rgb[0].permute(1,2,0)*255).detach().cpu().numpy().astype('uint8'))

def interp_poses(pose_md1, pose_md2, alpha = 0.5):
    candidate = []
    subset = [-1] * 20
    for i in range(18):
        j = int(pose_md1['subset'][0][i])
        k = int(pose_md2['subset'][0][i])
        if j == -1 or k == -1:
            candidate.append([-1,-1,0,i])
            subset[i] = -1
            continue
        candidate.append([pose_md1['candidate'][j][0] * alpha + pose_md2['candidate'][k][0] * (1-alpha),
            pose_md1['candidate'][j][1] * alpha + pose_md2['candidate'][k][1] * (1-alpha),
            0,i])
        subset[i] = i
    canvas = np.zeros((512, 512, 3), dtype=np.uint8)
    canvas = util.draw_bodypose(canvas, np.array(candidate), np.array([subset]))
    return canvas

class ContextManager:
    def __init__(self):
        self.filters = {}
        self.mode = None
        self.model = create_model('./controlnet/models/cldm_v15.yaml').cuda()
        self.ddim_sampler = DDIMSampler(self.model)

    def change_mode(self, mode):
        if self.mode == mode:
            return
        
        if mode not in self.filters:
            if mode == 'pose':
                self.filters[mode] = OpenposeDetector()
            elif mode == 'canny':
                self.filters[mode] = CannyDetector()
            elif mode == 'seg':
                self.filters[mode] = UniformerDetector()
        
        if mode == 'pose':
            self.model.load_state_dict(load_state_dict('./controlnet/models/control_sd15_openpose.pth', location='cuda'))
        elif mode == 'canny':
            self.model.load_state_dict(load_state_dict('./controlnet/models/control_sd15_canny.pth', location='cuda'))
        elif mode == 'seg':
            self.model.load_state_dict(load_state_dict('./controlnet/models/control_sd15_seg.pth', location='cuda'))

    def get_canny(self, image, lower_bound=220, upper_bound=255):
        self.change_mode('canny')
        canny = self.filters['canny'](HWC3(np.array(image)), lower_bound, upper_bound)
        return canny
        
    def get_pose(self, image, return_metadata=False):
        self.change_mode('pose')
        pred_pose, metadata = self.filters['pose'](HWC3(np.array(image)))
        if return_metadata:
            return pred_pose, metadata
        return pred_pose
        
    @torch.no_grad()
    def interpolate_pose(self, img1, pose_md1, img2, pose_md2, prompt, n_prompt,
                    T=1000, steps_per_frame=20, ddim_steps=50, guide_scale=7.5,
                    ctrl_scale=1.0):
        if isinstance(img1, Image.Image):
            img1 = torch.tensor(np.array(img1)).float().cuda() / 127.5 - 1.0
            img2 = torch.tensor(np.array(img2)).float().cuda() / 127.5 - 1.0
        self.change_mode('pose')
        ldm = self.model
        timesteps = self.ddim_sampler.ddim_timesteps
        T = len(timesteps)
        total_frames = 2**(T // self.steps_per_frame) + 1
        latents1, latents2 = self.get_latent_stack(img1, img2, num_frames)
        step = total_frames - 1
        
        num_frames = steps_per_frame * sum([2**x-1 for x in range(1, T//steps_per_frame+1)])
        latents = [None] * num_frames
        for i in trange(num_frames):
            if i % steps_per_frame == 0:
                latents[0] = latents1[-i//steps_per_frame-1]
                latents[-1] = latents2[-i//steps_per_frame-1]
                step //= 2

                if i // steps_per_frame == 2:
                    latents[total_frames // 2] = interpolate_spherical(
                        latents[total_frames // 4], latents[3 * total_frames // 4], .5)
                        
                for frame_ix in range(step, total_frames-1, step*2):
                    assert latents[frame_ix] is None
                    frac = .5
                    pose_img = interp_poses(pose_md1, pose_md2, alpha=frac).transpose(2,0,1)
                    if frame_ix-step == 0:
                        frac -= .15
                    if frame_ix+step == total_frames-1:
                        frac += .15
                    latents[frame_ix] = interpolate_spherical(
                        latents[frame_ix-step], latents[frame_ix+step], frac)

                    out = self.img2img(control=pose_img, noisy_latents=latents[frame_ix],
                                    mode='pose', prompt=prompt, n_prompt=n_prompt, ctrl_scale=ctrl_scale,
                                    guide_scale=guide_scale)
                    Image.fromarray(out).save(f'blend/{frame_ix:02d}.png')

    def get_latent_stack(self, img1, img2, num_frames):
        ldm = self.model
        latents1 = ldm.get_first_stage_encoding(ldm.encode_first_stage(img1.permute(2,0,1).unsqueeze(0)))
        latents2 = ldm.get_first_stage_encoding(ldm.encode_first_stage(img2.permute(2,0,1).unsqueeze(0)))
        
        ldm.num_timesteps
        shutil.rmtree('blend', ignore_errors=True)
        os.makedirs('blend')
        for frame in range(1,num_frames):
            alpha = frame/num_frames
            t_now += 100
            noise = torch.randn_like(latents1[-1])
            latents1.append(self.add_more_noise(latents1[-1], noise, t_now, t_prev))
            latents2.append(self.add_more_noise(latents2[-1], noise, t_now, t_prev))
            t_prev = t_now
        return latents1, latents2
    
    def add_more_noise(self, latents, noise, t2, t1=None):
        ldm = self.model
        if t1 is None:
            return ldm.sqrt_alphas_cumprod[t2] * latents + \
                ldm.sqrt_one_minus_alphas_cumprod[t2] * noise

        a1 = ldm.sqrt_alphas_cumprod[t1]
        sig1 = ldm.sqrt_one_minus_alphas_cumprod[t1]
        a2 = ldm.sqrt_alphas_cumprod[t2]
        sig2 = ldm.sqrt_one_minus_alphas_cumprod[t2]

        scale = a2/a1
        sigma = (sig2**2 - (scale * sig1)**2).sqrt()
        return scale * latents + sigma * noise
    
    @torch.no_grad()
    def img2img(self, control, prompt, n_prompt, init_img=None, noisy_latents=None, noise=None, mode=None, time_frac=0.3,
                ddim_steps=50, ctrl_scale=1, guide_scale=7.5, eta=0):
        if mode is not None:
            self.change_mode(mode)
        elif self.mode is None:
            print('no mode set')
            return
        
        if not isinstance(control, torch.Tensor):
            control = torch.from_numpy(control).float().cuda().unsqueeze(0) / 255.0
            if len(control.shape) == 3:
                control = control.tile(1, 3, 1, 1)

        ldm = self.model
        if noisy_latents is None:
            if init_img is not None:
                if isinstance(init_img, Image.Image):
                    init_img = torch.tensor(np.array(init_img)).float().cuda() / 127.5 - 1.0
                latents = ldm.get_first_stage_encoding(ldm.encode_first_stage(init_img.permute(2,0,1).unsqueeze(0)))
            
            T = int(time_frac * ldm.num_timesteps)
            t = torch.tensor([T], dtype=torch.long, device='cuda')
            noise = torch.randn_like(latents)
            noisy_latents = (extract_into_tensor(ldm.sqrt_alphas_cumprod, t, latents.shape) * latents +
                extract_into_tensor(ldm.sqrt_one_minus_alphas_cumprod, t, latents.shape) * noise)
            
        cond = {"c_concat": [control], "c_crossattn": [ldm.get_learned_conditioning([prompt])]}
        un_cond = {"c_concat": [control], "c_crossattn": [ldm.get_learned_conditioning([n_prompt])]}

        H = W = 512
        shape = (4, H // 8, W // 8)

        ldm.control_scales = [ctrl_scale] * 13
        samples, _ = self.ddim_sampler.sample(ddim_steps, 1,
            shape, cond, verbose=False, eta=eta, x_T=noisy_latents, timesteps=int(time_frac * ddim_steps),
            unconditional_guidance_scale=guide_scale,
            unconditional_conditioning=un_cond)

        x_samples = ldm.decode_first_stage(samples).permute(0, 2, 3, 1)
        x_samples = (x_samples * 127.5 + 127.5).cpu().numpy().clip(0, 255).astype(np.uint8)
        return x_samples[0]

    @torch.no_grad()
    def generate(self, control, prompt, n_prompt, num_samples=1,
                ddim_steps=50, ctrl_scale=1, guide_scale=7.5, eta=0):
        control = torch.from_numpy(control).float().cuda() / 255.0
        control = torch.stack([control for _ in range(num_samples)], dim=0)
        control = control.permute(0, 3, 1, 2)

        cond = {"c_concat": [control], "c_crossattn": [self.model.get_learned_conditioning([prompt] * num_samples)]}
        un_cond = {"c_concat": [control], "c_crossattn": [self.model.get_learned_conditioning([n_prompt] * num_samples)]}

        H = W = 512
        shape = (4, H // 8, W // 8)

        self.model.control_scales = [ctrl_scale] * 13
        samples, _ = self.ddim_sampler.sample(ddim_steps, num_samples,
                                shape, cond, verbose=False, eta=eta,
                                unconditional_guidance_scale=guide_scale,
                                unconditional_conditioning=un_cond)

        x_samples = self.model.decode_first_stage(samples).permute(0, 2, 3, 1)
        x_samples = (x_samples * 127.5 + 127.5).cpu().numpy().clip(0, 255).astype(np.uint8)
        return [x_samples[i] for i in range(num_samples)]

def main():
    cm = ContextManager()
    img1 = Image.open('05.png').convert('RGB').resize((512, 512))
    img2 = Image.open('j1.jpeg').convert('RGB').resize((512, 512))
    pose1 = cm.get_pose(img1)
    pose2 = cm.get_pose(img2)

    prompt = 'mulan, disney cartoon, extremely detailed, ultra hd'
    num_samples = 1
    strength = 1
    ddim_steps = 60
    scale = 9
    n_prompt = 'long body, lowres, bad anatomy, bad hands, missing fingers, extra digit, fewer digits, cropped, bad art, poorly drawn, low quality'
    cm.interpolate()
