#
# Copyright (c) 2021-2022, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import ctypes
from cuda import cudart
from time import time_ns
import numpy as np
import os
import tensorrt as trt

soFile = "./UpsamplePlugin.so"
np.random.seed(97)

def printArrayInfomation(x, info="", n=5):
    print( '%s:%s,SumAbs=%.5e,Var=%.5f,Max=%.5f,Min=%.5f,SAD=%.5f'%( \
        info,str(x.shape),np.sum(abs(x)),np.var(x),np.max(x),np.min(x),np.sum(np.abs(np.diff(x.reshape(-1)))) ))
    print('\t', x.reshape(-1)[:n], x.reshape(-1)[-n:])

def check(a, b, weak=False, checkEpsilon=1e-5):
    if weak:
        res = np.all(np.abs(a - b) < checkEpsilon)
    else:
        res = np.all(a == b)
    diff0 = np.max(np.abs(a - b))
    diff1 = np.max(np.abs(a - b) / (np.abs(b) + checkEpsilon))
    print("check:%s, absDiff=%f, relDiff=%f" % (res, diff0, diff1))

def addScalarCPU(inputH, scalar):
    return [inputH[0] + scalar]

def getUpsamplePlugin(nScaleFactor, bNearest):
    for c in trt.get_plugin_registry().plugin_creator_list:
        #print(c.name)
        if c.name == "Upsample_Plugin":
            parameterList = []
            parameterList.append(trt.PluginField("nScaleFactor", np.int32(nScaleFactor), trt.PluginFieldType.INT32))
            parameterList.append(trt.PluginField("bNearest", np.int32(bNearest), trt.PluginFieldType.INT32))
            return c.create_plugin(c.name, trt.PluginFieldCollection(parameterList))
    return None

def run(shape, nScaleFactor, bNearest):
    testCase = "<shape=%s,nScaleFactor=%d,bNearest=%d>" % (shape, nScaleFactor, bNearest)
    trtFile = "./model-Dim%s.plan" % str(len(shape))
    print("Test %s" % testCase)
    logger = trt.Logger(trt.Logger.ERROR)
    trt.init_libnvinfer_plugins(logger, '')
    ctypes.cdll.LoadLibrary(soFile)
    if os.path.isfile(trtFile):
        with open(trtFile, "rb") as f:
            engine = trt.Runtime(logger).deserialize_cuda_engine(f.read())
        if engine == None:
            print("Failed loading engine!")
            return
        print("Succeeded loading engine!")
    else:
        builder = trt.Builder(logger)
        network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        profile = builder.create_optimization_profile()
        config = builder.create_builder_config()
        config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 6 << 30)

        inputT0 = network.add_input("inputT0", trt.float32, [-1, 3, -1, -1])
        profile.set_shape(inputT0.name, [1, 3, 1, 1], [4, 3, 256, 256], [8, 3, 256, 256])
        config.add_optimization_profile(profile)

        plugin = getUpsamplePlugin(nScaleFactor, bNearest)
        pluginLayer = network.add_plugin_v2([inputT0], plugin)

        network.mark_output(pluginLayer.get_output(0))
        engineString = builder.build_serialized_network(network, config)
        if engineString == None:
            print("Failed building engine!")
            return
        print("Succeeded building engine!")
        with open(trtFile, "wb") as f:
            f.write(engineString)
        engine = trt.Runtime(logger).deserialize_cuda_engine(engineString)

    context = engine.create_execution_context()
    context.set_binding_shape(0, shape)
    #print("Binding all? %s"%(["No","Yes"][int(context.all_binding_shapes_specified)]))
    nInput = np.sum([engine.binding_is_input(i) for i in range(engine.num_bindings)])
    nOutput = engine.num_bindings - nInput
    #for i in range(engine.num_bindings):
    #    print("Bind[%2d]:i[%d]->"%(i,i) if engine.binding_is_input(i) else "Bind[%2d]:o[%d]->"%(i,i-nInput),
    #            engine.get_binding_dtype(i),engine.get_binding_shape(i),context.get_binding_shape(i),engine.get_binding_name(i))

    bufferH = []
    data = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    bufferH.append(data)
    for i in range(nOutput):
        bufferH.append(np.empty(context.get_binding_shape(nInput + i), dtype=trt.nptype(engine.get_binding_dtype(nInput + i))))
    bufferD = []
    for i in range(engine.num_bindings):
        bufferD.append(cudart.cudaMalloc(bufferH[i].nbytes)[1])

    for i in range(nInput):
        cudart.cudaMemcpy(bufferD[i], np.ascontiguousarray(bufferH[i].reshape(-1)).ctypes.data, bufferH[i].nbytes, cudart.cudaMemcpyKind.cudaMemcpyHostToDevice)

    context.execute_v2(bufferD)

    for i in range(nOutput):
        cudart.cudaMemcpy(bufferH[nInput + i].ctypes.data, bufferD[nInput + i], bufferH[nInput + i].nbytes, cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost)

    #outputCPU = addScalarCPU(bufferH[:nInput], scalar)

    for i in range(nInput):
        printArrayInfomation(bufferH[i])
        print(bufferH[i])
    for i in range(nOutput):
        printArrayInfomation(bufferH[i])
        print(bufferH[nInput + i])
    #for i in range(nOutput):
    #    printArrayInfomation(outputCPU[i])
    #check(bufferH[nInput:][0], outputCPU[0], True)

    ### warm up
    for i in range(10):
        # context.execute_async_v2(bufferD, stream)
        # cudart.cudaStreamSynchronize(stream)
        context.execute_v2(bufferD)

    ### test infernece time
    iteration = 100
    t0 = time_ns()
    for i in range(iteration):
        # context.execute_async_v2(bufferD, stream)
        # cudart.cudaStreamSynchronize(stream)
        context.execute_v2(bufferD)
    t1 = time_ns()
    timePerInference = (t1 - t0) / 1000 / 1000 / iteration
    print("inference latency: ", timePerInference)

    for buffer in bufferD:
        cudart.cudaFree(buffer)
    print("Test %s finish!\n" % testCase)

if __name__ == "__main__":
    os.system("rm -rf ./*.plan")
    np.set_printoptions(precision=3, linewidth=100, suppress=True)
    run([1, 3, 256, 256], 2, 0)

    print("Test all finish!")
