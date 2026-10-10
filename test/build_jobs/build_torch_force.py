"""Package a small periodic, mark-dependent pair force for later GPU execution."""
import os
import argparse
from pathlib import Path
import sys

os.environ['TORCHINDUCTOR_COMPILE_THREADS'] = '1'
os.environ['MAX_JOBS'] = '2'
# A runtime wheel is not a complete development toolkit. Declare the SDK once,
# as any native build does, instead of changing MCP for individual missing files.
parser = argparse.ArgumentParser()
parser.add_argument('--cuda-home', type=Path, default=Path(sys.base_prefix) / 'targets/x86_64-linux')
parser.add_argument('--cpu-only', action='store_true')
options = parser.parse_args()
required = ('cuda.h', 'cuda_runtime_api.h', 'crt/host_defines.h')
if not all((options.cuda_home / 'include' / name).exists() for name in required):
    raise RuntimeError('Provide --cuda-home pointing to a complete CUDA development toolkit')
os.environ['CUDA_HOME'] = str(options.cuda_home)
import torch


class PairForce(torch.nn.Module):
    def forward(self, positions, marks, ids, box):
        xyz = positions[:, :3]
        displacement = xyz[:, None, :] - xyz[None, :, :]
        displacement = displacement - box * torch.round(displacement / box)
        r2 = (displacement * displacement).sum(-1)
        ordered = marks[ids.to(torch.int64)]
        mask = (1 - torch.eye(positions.shape[0], device=positions.device)) * (r2 < 2.25)
        weight = 0.2 * ordered[:, None] * ordered[None, :] * torch.exp(-r2) * mask
        force = (-2 * weight[:, :, None] * displacement).sum(1)
        energy = (-0.5 * weight.sum()).to(torch.float64).reshape(1)
        return force, energy


artifact = Path(os.environ['GPU_MCP_ARTIFACT_PATH'])
artifact.parent.mkdir(parents=True, exist_ok=True)
model = PairForce()
n = 16
if options.cpu_only:
    # Keep the output target CUDA; never silently compile a CPU model instead.
    from torch._subclasses.fake_tensor import FakeTensorMode
    with FakeTensorMode():
        args = (torch.zeros((n, 4), device='cuda'), torch.ones(n, device='cuda'),
                torch.arange(n, dtype=torch.int32, device='cuda'), torch.ones(1, device='cuda'))
        exported = torch.export.export(model, args)
else:
    args = (torch.zeros((n, 4), device='cuda'), torch.ones(n, device='cuda'),
            torch.arange(n, dtype=torch.int32, device='cuda'), torch.ones(1, device='cuda'))
    exported = torch.export.export(model, args)
torch._inductor.aoti_compile_and_package(exported, package_path=str(artifact))
print(f'Packaged CUDA pair force: {artifact}')
