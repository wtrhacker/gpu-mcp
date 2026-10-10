"""Load the built extension using the ordinary guarded Python GPU runner."""
import json
from pathlib import Path
import sys

import torch

artifact = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(artifact.parent))
import build_cuda_probe

x = torch.arange(257, dtype=torch.float32, device='cuda') * 0.03125
y = torch.empty_like(x)
torch.cuda.synchronize()
build_cuda_probe.run(x.data_ptr(), y.data_ptr(), x.numel())
error = float((y - x*x).abs().max())
assert error < 1e-6
Path(sys.argv[2]).write_text(json.dumps({'passed': True, 'max_error': error, 'gpu': torch.cuda.get_device_name()}))
