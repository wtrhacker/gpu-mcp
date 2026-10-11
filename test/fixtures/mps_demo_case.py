"""Small real CUDA workload for the human MPS demonstration."""
import argparse
import json
from pathlib import Path
import time


def publish(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', required=True)
    parser.add_argument('--phase', choices=['smoke', 'main'], required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (root / f'attempt-{attempt}').exists():
        attempt += 1
    folder = root / f'attempt-{attempt}'
    folder.mkdir()

    import torch
    matrix = torch.ones((64, 64), device='cuda')
    start = time.monotonic()
    steps, last_report = 0, -10
    while True:
        result = matrix @ matrix
        torch.cuda.synchronize()
        value = float(result[0, 0])
        if value != 64.0:
            raise RuntimeError(f'CUDA result mismatch: {value}')
        steps += 1
        elapsed = time.monotonic() - start
        observation = {'case': args.case, 'attempt': attempt, 'steps': steps,
                       'elapsed_sec': round(elapsed, 2), 'value': value}
        publish(folder / 'checkpoint.json', observation)
        if elapsed - last_report >= 10 or elapsed >= args.seconds:
            print('GPU_MCP_PROGRESS ' + json.dumps({
                'current': min(int(elapsed), args.seconds), 'total': args.seconds,
                'message': f'{args.case}: CUDA checks passing; attempt {attempt}'}), flush=True)
            last_report = elapsed
        if args.phase == 'main' and args.case == 'contrast' and attempt == 1 and elapsed >= 45:
            publish(folder / 'failure.json', dict(observation, recoverable=True))
            raise RuntimeError('Recoverable input staging error. This attempt prepared its input; '
                               'one explicit retry of this case with unchanged arguments can continue.')
        if elapsed >= args.seconds:
            publish(folder / 'summary.json', dict(observation, passed=True))
            publish(root / 'summary.json', dict(observation, passed=True))
            print(f'{args.case} completed: {steps} CUDA checks, value={value}, attempt={attempt}', flush=True)
            return
        time.sleep(.5)


if __name__ == '__main__':
    main()
