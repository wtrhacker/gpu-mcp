# Scientific Workflow Display Attribution

`gpu_mcp_monitor_display.py` adapts the small progress-formatting and duration
functions, status palette and bounded message display from Dingyi Sun's
[Scientific Workflow](https://github.com/dingyisun0101/Scientific-Workflow),
version 0.15.0, `rust/src/ui/terminal.rs` and `rust/src/ui/state.rs`.

The Python terminal viewer consumes GPU MCP snapshots. It does not import the
Scientific Workflow runtime, scheduler, recording system or cancellation
controls. Closing the viewer leaves the backend and its jobs running.

The upstream MIT license is preserved in [LICENSE](LICENSE).

The display design was reviewed again on 2026-10-08 against Rust 0.16.1 / Python
0.6.1, commit
[`c6e788eb4a8e2e8c52cd1a4260b7949fe8a95e2d`](https://github.com/dingyisun0101/Scientific-Workflow/tree/c6e788eb4a8e2e8c52cd1a4260b7949fe8a95e2d).
Descriptive task labels, a separate allocation summary and retained page
selection inform the GPU MCP viewer. The new recording/receipt generation and
resource sampler are not imported. Original adapted functions retain the
0.15.0 attribution above.
