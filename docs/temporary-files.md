# Runtime temporary files

The worker creates a fresh private `.gpu-mcp-tmp-*` directory inside a single
`.gpu_mcp_tmp/<execution-host>/` container beneath the first approved write root for each job or
MPS case. For `write_roots = ["."]`, only `.gpu_mcp_tmp/` appears at the project
root. `GPU_MCP_TEMP_ROOT`, `TMPDIR`, and Python's
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
after library shutdown handlers. Failed jobs retain it for seven days and print
its path. Jobs killed without normal interpreter shutdown can also leave it
behind. A new worker checks for expired scratch before installing its job guard;
there is no separate cleanup service. Cleanup therefore happens on a subsequent
job, rather than exactly at the seven-day boundary.

Each scratch folder has a small `.owner.json` containing its host, PID, process
start identity, boot identity and creation/finish time. The worker holds that
file's exclusive lock until exit. Cleanup requires an unused lock, an expired
retention period and proof that the folder's PID ended on its matching execution
host. Host subdirectories and PID-prefixed folder names bind that check
independently of editable metadata flags. A live reused PID is conservatively
retained. Live processes,
unreadable identities, other hosts' unverified owners, malformed metadata and
symlinks are retained. Collection pins directory descriptors and checks the
folder's device/inode again before removal. Creation and recursive removal use
pinned directory handles, so replacing a pathname cannot redirect them into a
different directory. Ordinary outputs and the container
itself never receive the job's temporary delete/rename grant.

Existing top-level scratch can be moved into the container after checking its
exact paths on the execution hosts and proving its users have ended. A verified
migration goes under an inspected execution host and receives seven days from
the move, preserving its contents and original folder name. A migration marker
never overrides the host/PID and lock checks. Unmarked legacy folders are not
removed automatically. Active scratch
must stay at its original path until its owner finishes.

Libraries with separate compiler-cache settings should put those caches below
`GPU_MCP_TEMP_ROOT` when they require cleanup or atomic replacement. This fixes
the observed Python `tempfile` failure during a Torch compiler-related import.
It does not grant compiler subprocess execution or establish that every
`torch.compile` backend works under the remaining worker restrictions.
