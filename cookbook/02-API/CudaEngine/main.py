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

import numpy as np
from cuda import cudart
import tensorrt as trt

nB, nC, nH, nW = 1, 4, 8, 8  # nC % 4 ==0，全部值得到保存
#nB, nC, nH, nW = 1, 3, 8, 8  # nC % 4 !=0，会丢值
data = (np.arange(1, 1 + nB * nC * nH * nW, dtype=np.float32) / np.prod(nB * nC * nH * nW) * 128).astype(np.float32).reshape(nB, nC, nH, nW)

np.set_printoptions(precision=3, edgeitems=8, linewidth=300, suppress=True)
cudart.cudaDeviceSynchronize()

logger = trt.Logger(trt.Logger.ERROR)
builder = trt.Builder(logger)
network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
profile = builder.create_optimization_profile()
config = builder.create_builder_config()
config.flags = 1 << int(trt.BuilderFlag.INT8)  # 打开 int8 模式
config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
inputT0 = network.add_input("inputT0", trt.float32, (-1, nC, nH, nW))
profile.set_shape(inputT0.name, [1, nC, nH, nW], [nB, nC, nH, nW], [nB * 2, nC, nH, nW])
config.add_optimization_profile(profile)

layer = network.add_identity(inputT0)
layer.get_output(0).dtype = trt.int8
layer.set_output_type(0, trt.int8)
layer.get_output(0).allowed_formats = 1 << int(trt.TensorFormat.CHW4)
layer.get_output(0).dynamic_range = [-128, 128]

network.mark_output(layer.get_output(0))
engineString = builder.build_serialized_network(network, config)
engine = trt.Runtime(logger).deserialize_cuda_engine(engineString)
context = engine.create_execution_context()
nInput = np.sum([engine.binding_is_input(i) for i in range(engine.num_bindings)])
nOutput = engine.num_bindings - nInput

print("engine.device_memory_size = %d" % engine.device_memory_size)
#print("engine.max_workspace_size = %d" % engine.max_workspace_size)  # deprecated since TensorRT 8.4
print("engine.engine_capability = %d" % engine.engine_capability)
print("engine.has_implicit_batch_dimension = %s" % engine.has_implicit_batch_dimension)
print("engine.max_batch_size = %d" % engine.max_batch_size)
print("engine.name = %s" % engine.name)
print("engine.num_bindings = %d" % engine.num_bindings)
print("engine.num_layers = %d" % engine.num_layers)
print("engine.num_optimization_profiles = %d" % engine.num_optimization_profiles)
print("engine.refittable = %s" % engine.refittable)
print("engine.tactic_sources = %d" % engine.tactic_sources)

print("engine.__len__() = %d" % len(engine))
print("engine.__sizeof__() = %d" % engine.__sizeof__())
print("engine.__str__() = %s" % engine.__str__())
print("\n\nMethod related to binding:")
print("Binding:                           %s 0,%s 1" % (" " * 56, " " * 56))
print("get_binding_name:                  %58s,%58s" % (engine.get_binding_name(0), engine.get_binding_name(1)))
print("get_binding_shape:                 %58s,%58s" % (engine.get_binding_shape(0), engine.get_binding_shape(1)))
print("get_binding_dtype:                 %58s,%58s" % (engine.get_binding_dtype(0), engine.get_binding_dtype(1)))
print("get_binding_format:                %58s,%58s" % (engine.get_binding_format(0), engine.get_binding_format(1)))
print("get_binding_format_desc:           %58s,%58s" % (engine.get_binding_format_desc(0), engine.get_binding_format_desc(1)))
print("get_binding_bytes_per_component:   %58d,%58d" % (engine.get_binding_bytes_per_component(0), engine.get_binding_bytes_per_component(1)))
print("get_binding_components_per_element:%58d,%58d" % (engine.get_binding_components_per_element(0), engine.get_binding_components_per_element(1)))
print("get_binding_vectorized_dim:        %58d,%58d" % (engine.get_binding_vectorized_dim(0), engine.get_binding_vectorized_dim(1)))
print("")
print("binding_is_input:                  %58s,%58s" % (engine.binding_is_input(0), engine.binding_is_input(1)))
print("is_execution_binding:              %58s,%58s" % (engine.is_execution_binding(0), engine.is_execution_binding(1)))
print("is_shape_binding:                  %58s,%58s" % (engine.is_shape_binding(0), engine.is_shape_binding(1)))
print("get_profile_shape:                 %58s,%58s" % (engine.get_profile_shape(0, 0), ""))  # 只有输入张量才有 Optimization Profile Shape
#print("get_profile_shape:                 %58s,%58s" % (engine.get_profile_shape_input(0,0), engine.get_profile_shape_input(0,1)))  # 针对 Shape Tensor 的，这个模型中没有
print("__getitem__(int):                  %58s,%58s" % (engine[0], engine[1]))
print("__getitem__(str):                  %58d,%58d" % (engine["inputT0"], engine["(Unnamed Layer* 0) [Identity]_output"]))
print("get_binding_index:                 %58d,%58d" % (engine.get_binding_index("inputT0"), engine.get_binding_index("(Unnamed Layer* 0) [Identity]_output")))

context.set_binding_shape(0, [nB, nC, nH, nW])

bufferH = []
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

for i in range(nInput):
    print("Input %d:" % i, bufferH[i].shape, "\n", bufferH[i])
for i in range(nOutput):
    print("Output %d:" % i, bufferH[nInput + i].shape, "\n", bufferH[nInput + i])

print("Restore to Linear:")
print(bufferH[-1].reshape(nB * nC * nH * 2, nW // 2).transpose(1, 0).reshape(nB, nC, nH, nW))

for buffer in bufferD:
    cudart.cudaFree(buffer)
"""
# ICudaEngine 的成员方法
# ++++ 表示代码中已经展示，==== 表示代码中作为 binding 进行展示，---- 表示代码中没有进行展示，无前缀表示其他内部方法
----__class__
__del__
__delattr__
__dir__
__doc__
__enter__
__eq__
__exit__
__format__
__ge__
__getattribute__
====__getitem__
__gt__
__hash__
__init__
__init_subclass__
__le__
++++__len__
__lt__
__module__
__ne__
__new__
__reduce__
__reduce_ex__
__repr__
__setattr__
++++__sizeof__
++++__str__
__subclasshook__
====binding_is_input
----create_engine_inspector
----create_execution_context
----create_execution_context_without_device_memory
++++device_memory_size
++++engine_capability
----error_recorder
====get_binding_bytes_per_component
====get_binding_components_per_element
====get_binding_dtype
====get_binding_format
====get_binding_format_desc
====get_binding_index
====get_binding_name
====get_binding_shape
====get_binding_vectorized_dim
====get_location
====get_profile_shape
====get_profile_shape_input
++++has_implicit_batch_dimension
====is_execution_binding
====is_shape_binding
++++max_batch_size
++++name
++++num_bindings
++++num_layers
++++num_optimization_profiles
----profiling_verbosity
++++refittable
----serialize
++++tactic_sources
"""
