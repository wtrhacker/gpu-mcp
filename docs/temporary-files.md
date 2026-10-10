# Runtime temporary files

The worker creates a fresh private `.gpu-mcp-tmp-*` directory beneath an
approved write root for each job. `GPU_MCP_TEMP_ROOT`, `TMPDIR`, and Python's
cached temporary-directory setting point to it. Libraries can create/delete
temporary files there and atomically replace cache entries within that directory.

The exception covers `os.remove`, `os.rmdir`, `shutil.rmtree`, and `os.rename`
(including `os.replace`). Both ends of a rename must remain inside this job's
temporary directory. Directory descriptors and symlinked parent paths are
resolved before allowing cleanup. Changing an environment variable does not
change the captured permission boundary.

Project files and other approved output directories retain their existing
delete/rename restrictions. Entry-script scanning and subprocess, shell,
network and permission-change restrictions are unchanged.

Successful jobs remove their private temporary directory at interpreter exit,
after library shutdown handlers. Failed jobs retain it for diagnosis and print
its path. Jobs killed without normal interpreter shutdown can also leave it
behind. Ordinary job outputs are never part of this cleanup.

Libraries with separate compiler-cache settings should put those caches below
`GPU_MCP_TEMP_ROOT` when they require cleanup or atomic replacement. This fixes
the observed Python `tempfile` failure during a Torch compiler-related import.
It does not grant compiler subprocess execution or establish that every
`torch.compile` backend works under the remaining worker restrictions.
