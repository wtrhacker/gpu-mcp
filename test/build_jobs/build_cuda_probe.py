"""Build a tiny CUDA Python extension using CPU-only NVRTC and a C compiler."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile

artifact = Path(os.environ['GPU_MCP_ARTIFACT_PATH'])
artifact.parent.mkdir(parents=True, exist_ok=True)
arch = sys.argv[1] if len(sys.argv) > 1 else 'compute_80'
candidates = [Path(sys.prefix) / 'lib/libnvrtc.so', Path('/usr/local/cuda/lib64/libnvrtc.so')]
candidates += sorted((Path(sysconfig.get_paths()['purelib']) / 'nvidia/cuda_nvrtc/lib').glob('libnvrtc.so*'))
nvrtc_path = next(path for path in candidates if path.exists())
nvrtc = ctypes.CDLL(str(nvrtc_path))
program = ctypes.c_void_p()
source = b'extern "C" __global__ void square(const float* x, float* y, int n) { int i=blockIdx.x*blockDim.x+threadIdx.x; if(i<n) y[i]=x[i]*x[i]; }'
assert nvrtc.nvrtcCreateProgram(ctypes.byref(program), source, b'probe.cu', 0, None, None) == 0
options = (ctypes.c_char_p * 1)(f'--gpu-architecture={arch}'.encode())
rc = nvrtc.nvrtcCompileProgram(program, 1, options)
if rc:
    size = ctypes.c_size_t()
    nvrtc.nvrtcGetProgramLogSize(program, ctypes.byref(size))
    log = ctypes.create_string_buffer(size.value)
    nvrtc.nvrtcGetProgramLog(program, log)
    raise RuntimeError(log.value.decode())
size = ctypes.c_size_t()
nvrtc.nvrtcGetPTXSize(program, ctypes.byref(size))
ptx = ctypes.create_string_buffer(size.value)
nvrtc.nvrtcGetPTX(program, ptx)
nvrtc.nvrtcDestroyProgram(ctypes.byref(program))

c_source = r'''
#include <Python.h>
#include <dlfcn.h>
typedef void* Module;
typedef void* Function;
static const char* ptx = PTX_TEXT;
static PyObject* run(PyObject* self, PyObject* args) {
    unsigned long long x, y;
    int n;
    if (!PyArg_ParseTuple(args, "KKi", &x, &y, &n)) return NULL;
    void* lib = dlopen("libcuda.so.1", RTLD_NOW);
    if (!lib) return PyErr_Format(PyExc_RuntimeError, "CUDA driver: %s", dlerror());
    int (*load)(Module*, const void*) = dlsym(lib, "cuModuleLoadData");
    int (*get)(Function*, Module, const char*) = dlsym(lib, "cuModuleGetFunction");
    int (*launch)(Function,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned,void*,void**,void**) = dlsym(lib,"cuLaunchKernel");
    int (*sync)(void) = dlsym(lib,"cuCtxSynchronize");
    int (*unload)(Module) = dlsym(lib,"cuModuleUnload");
    if (!load || !get || !launch || !sync || !unload) return PyErr_Format(PyExc_RuntimeError,"CUDA symbols unavailable");
    Module module;
    Function function;
    int rc = load(&module, ptx);
    if (rc) return PyErr_Format(PyExc_RuntimeError,"CUDA module load: %d",rc);
    rc = get(&function,module,"square");
    void* argv[] = {&x,&y,&n};
    if (!rc) rc = launch(function,(n+127)/128,1,1,128,1,1,0,NULL,argv,NULL);
    if (!rc) rc = sync();
    unload(module);
    dlclose(lib);
    if (rc) return PyErr_Format(PyExc_RuntimeError,"CUDA execution: %d",rc);
    Py_RETURN_NONE;
}
static PyMethodDef methods[] = {{"run",run,METH_VARARGS,"Run compiled kernel"},{NULL,NULL,0,NULL}};
static struct PyModuleDef definition = {PyModuleDef_HEAD_INIT,"build_cuda_probe",NULL,-1,methods};
PyMODINIT_FUNC PyInit_build_cuda_probe(void) { return PyModule_Create(&definition); }
'''.replace('PTX_TEXT', json.dumps(ptx.value.decode()))
with tempfile.TemporaryDirectory() as work:
    source_path = Path(work) / 'module.c'
    source_path.write_text(c_source)
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-I' + sysconfig.get_paths()['include'],
                    str(source_path), '-ldl', '-o', str(artifact)], check=True)
print(json.dumps({'artifact': str(artifact), 'target': arch, 'nvrtc': str(nvrtc_path), 'gpu_used': False}))
