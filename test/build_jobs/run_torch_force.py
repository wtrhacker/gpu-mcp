"""Run a prebuilt Torch force under the ordinary GPU guard and check its inputs."""
import json
from pathlib import Path
import sys

import numpy as np
import torch

kernel = torch._inductor.aoti_load_package(sys.argv[1])
rng = np.random.default_rng(123)
positions = rng.uniform(0, 4, (16, 4)).astype(np.float32)
ids = np.arange(16, dtype=np.int32)[::-1].copy()
box = np.array([4], dtype=np.float32)
errors = []
for phase in range(3):
    marks = ((np.arange(16) + phase) % 2).astype(np.float32)
    positions[:, :3] += np.float32(0.037 * phase)
    xyz = positions[:, :3]
    displacement = xyz[:, None, :] - xyz[None, :, :]
    displacement -= box * np.round(displacement / box)
    r2 = (displacement * displacement).sum(-1)
    ordered = marks[ids]
    weight = 0.2 * ordered[:, None] * ordered[None, :] * np.exp(-r2)
    weight *= (1 - np.eye(16, dtype=np.float32)) * (r2 < 2.25)
    expected = (-2 * weight[:, :, None] * displacement).sum(1)
    forces, energy = kernel(torch.tensor(positions, device='cuda'), torch.tensor(marks, device='cuda'),
                            torch.tensor(ids, device='cuda'), torch.tensor(box, device='cuda'))
    error = float(np.max(np.abs(forces.cpu().numpy() - expected)))
    assert error < 2e-5
    assert abs(float(energy.cpu()[0]) + float(weight.sum()) * .5) < 2e-5
    errors.append(error)
Path(sys.argv[2]).write_text(json.dumps({'passed': True, 'max_force_error': max(errors),
                                      'phases': len(errors), 'torch': torch.__version__}))
