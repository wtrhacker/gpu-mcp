#!/usr/bin/env python3
"""Tiny GPU MCP launch probe."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
os.environ.setdefault("XLA_PYTHON_CLIENT_MEM_FRACTION", "0.05")


def try_jax() -> bool:
    try:
        import jax
        import jax.numpy as jnp
    except Exception as exc:  # pragma: no cover - probe script
        print(f"jax_import_error={type(exc).__name__}: {exc}")
        return False

    try:
        devices = jax.devices()
        print("jax_devices=" + ",".join(str(device) for device in devices))
        arr = jnp.arange(4096, dtype=jnp.float32)
        result = jnp.sum(arr * arr).block_until_ready()
        print(f"jax_result={float(result):.1f}")
        return any(device.platform == "gpu" for device in devices)
    except Exception as exc:  # pragma: no cover - probe script
        print(f"jax_runtime_error={type(exc).__name__}: {exc}")
        return False


def try_torch() -> bool:
    try:
        import torch
    except Exception as exc:  # pragma: no cover - probe script
        print(f"torch_import_error={type(exc).__name__}: {exc}")
        return False

    try:
        print(f"torch_cuda_available={torch.cuda.is_available()}")
        print(f"torch_cuda_device_count={torch.cuda.device_count()}")
        if not torch.cuda.is_available():
            return False
        device = torch.device("cuda:0")
        tensor = torch.arange(4096, dtype=torch.float32, device=device)
        result = torch.sum(tensor * tensor).item()
        print(f"torch_device_name={torch.cuda.get_device_name(0)}")
        print(f"torch_result={result:.1f}")
        return True
    except Exception as exc:  # pragma: no cover - probe script
        print(f"torch_runtime_error={type(exc).__name__}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=0.0)
    args = parser.parse_args()

    print(f"host={socket.gethostname()}")
    print(f"pid={os.getpid()}")
    print(f"cuda_visible_devices={os.environ.get('CUDA_VISIBLE_DEVICES', '')}")

    used_gpu = try_jax()
    if not used_gpu:
        used_gpu = try_torch()

    if args.sleep > 0:
        print(f"sleeping_seconds={args.sleep}")
        time.sleep(args.sleep)

    print(f"used_gpu={used_gpu}")
    return 0 if used_gpu else 1


if __name__ == "__main__":
    sys.exit(main())
