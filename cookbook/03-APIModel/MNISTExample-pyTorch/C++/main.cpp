/*
 * Copyright (c) 2021-2022, NVIDIA CORPORATION. All rights reserved.

 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "calibrator.h"
#include "cnpy.h"
#include "cookbookHelper.hpp"

using namespace nvinfer1;

const int         nHeight {28};
const int         nWidth {28};
const std::string paraFile {"../para.npz"};
const std::string trtFile {"./model.plan"};
const std::string dataFile {"./data.npz"};
static Logger gLogger(ILogger::Severity::kERROR);

// for FP16 mode
const bool bFP16Mode {false};
// for INT8 mode
const bool        bINT8Mode {false};
const int         nCalibration {1};
const std::string cacheFile {"./int8.cache"};
const std::string calibrationDataFile = std::string("./data.npz");
// 使用 00-MNISTData/test 的前 100 张图片作为校正数据，并将数据保存为 .npz 供校正器使用，避免图片解码相关代码，同理推理输入数据也保存进来

int main()
{
    ck(cudaSetDevice(0));
    ICudaEngine *engine = nullptr;

    if (access(trtFile.c_str(), F_OK) == 0)
    {
        std::ifstream engineFile(trtFile, std::ios::binary);
        long int      fsize = 0;

        engineFile.seekg(0, engineFile.end);
        fsize = engineFile.tellg();
        engineFile.seekg(0, engineFile.beg);
        std::vector<char> engineString(fsize);
        engineFile.read(engineString.data(), fsize);
        if (engineString.size() == 0)
        {
            std::cout << "Failed getting serialized engine!" << std::endl;
            return 1;
        }
        std::cout << "Succeeded getting serialized engine!" << std::endl;

        IRuntime *runtime {createInferRuntime(gLogger)};
        engine = runtime->deserializeCudaEngine(engineString.data(), fsize);
        if (engine == nullptr)
        {
            std::cout << "Failed loading engine!" << std::endl;
            return 1;
        }
        std::cout << "Succeeded loading engine!" << std::endl;
    }
    else
    {
        IBuilder *            builder = createInferBuilder(gLogger);
        INetworkDefinition *  network = builder->createNetworkV2(1U << int(NetworkDefinitionCreationFlag::kEXPLICIT_BATCH));
        IOptimizationProfile *profile = builder->createOptimizationProfile();
        IBuilderConfig *      config  = builder->createBuilderConfig();
        //config->setMemoryPoolLimit(MemoryPoolType::kWORKSPACE, 6 << 30);
        IInt8Calibrator *pCalibrator = nullptr;
        if (bFP16Mode)
        {
            config->setFlag(BuilderFlag::kFP16);
        }
        if (bINT8Mode)
        {
            config->setFlag(BuilderFlag::kINT8);
            Dims32 inputShape {4, {1, 1, nHeight, nWidth}};
            pCalibrator = new MyCalibrator(calibrationDataFile, nCalibration, inputShape, cacheFile);
            if (pCalibrator == nullptr)
            {
                std::cout << std::string("Failed getting Calibrator for Int8!") << std::endl;
                return 1;
            }
            config->setInt8Calibrator(pCalibrator);
        }

        ITensor *inputTensor = network->addInput("inputT0", DataType::kFLOAT, Dims32 {4, {-1, -1, nHeight, nWidth}});
        profile->setDimensions(inputTensor->getName(), OptProfileSelector::kMIN, Dims32 {4, {1, 1, nHeight, nWidth}});
        profile->setDimensions(inputTensor->getName(), OptProfileSelector::kOPT, Dims32 {4, {4, 1, nHeight, nWidth}});
        profile->setDimensions(inputTensor->getName(), OptProfileSelector::kMAX, Dims32 {4, {8, 1, nHeight, nWidth}});
        config->addOptimizationProfile(profile);

        cnpy::npz_t    npzFile = cnpy::npz_load(paraFile);
        cnpy::NpyArray array;
        Weights        w;
        Weights        b;

        array    = npzFile[std::string("conv1.weight")];
        w        = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        array    = npzFile[std::string("conv1.bias")];
        b        = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        auto *_0 = network->addConvolution(*inputTensor, 32, DimsHW {5, 5}, w, b);
        _0->setPaddingNd(DimsHW {2, 2});
        auto *_1 = network->addActivation(*_0->getOutput(0), ActivationType::kRELU);
        auto *_2 = network->addPooling(*_1->getOutput(0), PoolingType::kMAX, DimsHW {2, 2});
        _2->setStride(DimsHW {2, 2});

        array    = npzFile[std::string("conv2.weight")];
        w        = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        array    = npzFile[std::string("conv2.bias")];
        b        = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        auto *_3 = network->addConvolution(*_2->getOutput(0), 64, DimsHW {5, 5}, w, b);
        _3->setPaddingNd(DimsHW {2, 2});
        auto *_4 = network->addActivation(*_3->getOutput(0), ActivationType::kRELU);
        auto *_5 = network->addPooling(*_4->getOutput(0), PoolingType::kMAX, DimsHW {2, 2});
        _5->setStride(DimsHW {2, 2});

        auto *_6 = network->addShuffle(*_5->getOutput(0));
        _6->setReshapeDimensions(Dims32 {2, {-1, 64 * 7 * 7}});

        array     = npzFile[std::string("fc1.weight")];
        w         = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        array     = npzFile[std::string("fc1.bias")];
        b         = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        auto *_7  = network->addConstant(Dims32 {2, {1024, 64 * 7 * 7}}, w);
        auto *_8  = network->addMatrixMultiply(*_6->getOutput(0), MatrixOperation::kNONE, *_7->getOutput(0), MatrixOperation::kTRANSPOSE);
        auto *_9  = network->addConstant(Dims32 {2, {1, 1024}}, b);
        auto *_10 = network->addElementWise(*_8->getOutput(0), *_9->getOutput(0), ElementWiseOperation::kSUM);
        auto *_11 = network->addActivation(*_10->getOutput(0), ActivationType::kRELU);

        array     = npzFile[std::string("fc2.weight")];
        w         = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        array     = npzFile[std::string("fc2.bias")];
        b         = Weights {DataType::kFLOAT, array.data<float>(), array.num_bytes() / sizeof(float)};
        auto *_12 = network->addConstant(Dims32 {2, {10, 1024}}, w);
        auto *_13 = network->addMatrixMultiply(*_11->getOutput(0), MatrixOperation::kNONE, *_12->getOutput(0), MatrixOperation::kTRANSPOSE);
        auto *_14 = network->addConstant(Dims32 {2, {1, 10}}, b);
        auto *_15 = network->addElementWise(*_13->getOutput(0), *_14->getOutput(0), ElementWiseOperation::kSUM);

        auto *_16 = network->addSoftMax(*_15->getOutput(0));
        _16->setAxes(1U << 1);

        auto *_17 = network->addTopK(*_16->getOutput(0), TopKOperation::kMAX, 1, 1U << 1);

        network->markOutput(*_17->getOutput(1));

        IHostMemory *engineString = builder->buildSerializedNetwork(*network, *config);
        if (engineString == nullptr || engineString->size() == 0)
        {
            std::cout << "Failed building serialized engine!" << std::endl;
            return 1;
        }
        std::cout << "Succeeded building serialized engine!" << std::endl;

        IRuntime *runtime {createInferRuntime(gLogger)};
        engine = runtime->deserializeCudaEngine(engineString->data(), engineString->size());
        if (engine == nullptr)
        {
            std::cout << "Failed building engine!" << std::endl;
            return 1;
        }
        std::cout << "Succeeded building engine!" << std::endl;

        if (bINT8Mode && pCalibrator != nullptr)
        {
            delete pCalibrator;
        }
        /*
        std::ofstream engineFile(trtFile, std::ios::binary);
        if (!engineFile)
        {
            std::cout << "Failed opening file to write" << std::endl;
            return 1;
        }
        engineFile.write(static_cast<char *>(engineString->data()), engineString->size());
        if (engineFile.fail())
        {
            std::cout << "Failed saving .plan file!" << std::endl;
            return 1;
        }
        std::cout << "Succeeded saving .plan file!" << std::endl;
        */
    }

    IExecutionContext *context = engine->createExecutionContext();
    context->setBindingDimensions(0, Dims32 {4, {1, 1, nHeight, nWidth}});
    std::cout << std::string("Binding all? ") << std::string(context->allInputDimensionsSpecified() ? "Yes" : "No") << std::endl;
    int nBinding = engine->getNbBindings();
    int nInput   = 0;
    for (int i = 0; i < nBinding; ++i)
    {
        nInput += int(engine->bindingIsInput(i));
    }
    int nOutput = nBinding - nInput;
    for (int i = 0; i < nBinding; ++i)
    {
        std::cout << std::string("Bind[") << i << std::string(i < nInput ? "]:i[" : "]:o[") << (i < nInput ? i : i - nInput) << std::string("]->");
        std::cout << dataTypeToString(engine->getBindingDataType(i)) << std::string(" ");
        std::cout << shapeToString(context->getBindingDimensions(i)) << std::string(" ");
        std::cout << engine->getBindingName(i) << std::endl;
    }

    std::vector<int> vBindingSize(nBinding, 0);
    for (int i = 0; i < nBinding; ++i)
    {
        Dims32 dim  = context->getBindingDimensions(i);
        int    size = 1;
        for (int j = 0; j < dim.nbDims; ++j)
        {
            size *= dim.d[j];
        }
        vBindingSize[i] = size * dataTypeToSize(engine->getBindingDataType(i));
    }

    std::vector<void *> vBufferH {nBinding, nullptr};
    std::vector<void *> vBufferD {nBinding, nullptr};
    for (int i = 0; i < nBinding; ++i)
    {
        vBufferH[i] = (void *)new char[vBindingSize[i]];
        ck(cudaMalloc(&vBufferD[i], vBindingSize[i]));
    }

    cnpy::npz_t    npzFile = cnpy::npz_load(dataFile);
    cnpy::NpyArray array   = npzFile[std::string("inferenceData")];
    memcpy(vBufferH[0], array.data<float>(), vBindingSize[0]);

    for (int i = 0; i < nInput; ++i)
    {
        ck(cudaMemcpy(vBufferD[i], vBufferH[i], vBindingSize[i], cudaMemcpyHostToDevice));
    }

    context->executeV2(vBufferD.data());

    for (int i = nInput; i < nBinding; ++i)
    {
        ck(cudaMemcpy(vBufferH[i], vBufferD[i], vBindingSize[i], cudaMemcpyDeviceToHost));
    }

    printArrayInfomation((float *)vBufferH[0], context->getBindingDimensions(0), std::string(engine->getBindingName(0)));
    printArrayInfomation((int *)vBufferH[1], context->getBindingDimensions(1), std::string(engine->getBindingName(1)), true, 1);

    for (int i = 0; i < nBinding; ++i)
    {
        ck(cudaFree(vBufferD[i]));
    }

    return 0;
}
