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
import numpy as np
import os
import tensorrt as trt

soFile = "./CuBLASGemmPlugin.so"
b, m, k, n = 5, 2, 3, 4
np.random.seed(97)

globalData = np.random.rand(b * m * k).astype(np.float32).reshape(b, m, k) * 2 - 1
globalWeight = np.random.rand(k * n).astype(np.float32).reshape(k, n) * 2 - 1

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

def CuBLASGemmCPU(inputH, weight):
    return [np.matmul(inputH[0], weight)]

def getCuBLASGemmPlugin(weight):
    for c in trt.get_plugin_registry().plugin_creator_list:
        #print(c.name)
        if c.name == "CuBLASGemm":
            parameterList = []
            parameterList.append(trt.PluginField("weight", np.float32(weight), trt.PluginFieldType.FLOAT32))
            parameterList.append(trt.PluginField("k", np.int32(weight.shape[0]), trt.PluginFieldType.INT32))
            parameterList.append(trt.PluginField("n", np.int32(weight.shape[1]), trt.PluginFieldType.INT32))
            return c.create_plugin(c.name, trt.PluginFieldCollection(parameterList))
    return None

def run():
    trtFile = "./model.plan"
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

        inputT0 = network.add_input("inputT0", trt.float32, [-1, -1, k])
        profile.set_shape(inputT0.name, [1, 1, k], [b, m, k], [b * 2, m * 2, k])
        config.add_optimization_profile(profile)

        pluginLayer = network.add_plugin_v2([inputT0], getCuBLASGemmPlugin(globalWeight))
        pluginLayer.get_output(0).name = "GEMM-Plugin-Output"

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
    context.set_binding_shape(0, [b, m, k])
    #print("Binding all? %s"%(["No","Yes"][int(context.all_binding_shapes_specified)]))
    nInput = np.sum([engine.binding_is_input(i) for i in range(engine.num_bindings)])
    nOutput = engine.num_bindings - nInput
    #for i in range(engine.num_bindings):
    #    print("Bind[%2d]:i[%d]->"%(i,i) if engine.binding_is_input(i) else "Bind[%2d]:o[%d]->"%(i,i-nInput),
    #            engine.get_binding_dtype(i),engine.get_binding_shape(i),context.get_binding_shape(i),engine.get_binding_name(i))

    bufferH = []
    bufferH.append(globalData)
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

    outputCPU = CuBLASGemmCPU(bufferH[:nInput], globalWeight)

    for i in range(nInput):
        printArrayInfomation(bufferH[i])
    for i in range(nOutput):
        printArrayInfomation(bufferH[nInput + i])
    for i in range(nOutput):
        printArrayInfomation(outputCPU[i])

    check(bufferH[nInput:][0], outputCPU[0], True)

    for buffer in bufferD:
        cudart.cudaFree(buffer)

if __name__ == "__main__":
    os.system("rm -rf ./*.plan")
    np.set_printoptions(precision=3, linewidth=100, suppress=True)

    run()  # 创建 TensorRT 引擎并推理
    run()  # 读取 TensorRT 引擎并推理

    print("Test all finish!")