# VLA Model Compression Literature Review: Quantization, Pruning, and Knowledge Distillation

## Executive Summary

This literature review examines recent advances (2024-2026) in model compression techniques specifically applied to Vision-Language-Action (VLA) models and multimodal robotics systems. The research reveals significant progress in quantization, pruning, and knowledge distillation approaches that enable VLA model deployment on edge devices while maintaining competitive performance.

## Key Findings by Compression Technique

### 1. Quantization Approaches

#### BitVLA (2025) - 1-Bit Quantization
- **Compression Ratio**: 11.0x reduction in model memory
- **Latency Improvement**: 4.4x faster end-to-end inference  
- **Memory Footprint**: Only 1.4GB (deployable on RTX 3050 Ti with 4GB)
- **Weight Precision**: Fully ternary weights {-1, 0, 1}
- **Performance**: Matches full-precision OpenVLA-OFT on LIBERO benchmark
- **Architecture**: Built on BitNet b1.58 2B4T foundation
- **Innovation**: "Quantize-then-Distill" post-training strategy for vision encoders

#### OpenVLA Post-Training Quantization
- **FP8 Quantization**: Model size reduced to 7.68GB with no significant accuracy drop
- **NVFP4 Quantization**: Model size reduced to 4.44GB
- **4-bit Quantization**: Enables inference on consumer GPUs (RTX 4090) with negligible performance loss
- **Deployment**: Supports quantization for resource-constrained deployment

### 2. Pruning and Structural Compression

#### Compressor-VLA 
- **FLOP Reduction**: 59% reduction (from 3.95T to 1.62T FLOPs)
- **Token Compression**: Over 3x compression (512 to 160 tokens)
- **Performance**: 97.1% vs 97.3% success rate compared to OpenVLA-OFT baseline
- **Approach**: Instruction-guided visual token compression

#### SQAP-VLA - Spatially and Quantization-Aware Pruning
- **Method**: Token pruning framework preserving task-critical tokens
- **Focus**: Spatial coverage maintenance and robot end-effector protection
- **Integration**: Combined quantization and pruning approach

#### Large Multimodal Model Compression (2024)
- **Latency Improvement**: 700ms to 90ms (87% reduction)
- **Approach**: Multi-stage pruning addressing multi-level redundancy
- **Data Efficiency**: Recovery with only 5% of original training data
- **Performance Retention**: Over 95% of original performance maintained

### 3. Knowledge Distillation

#### Structural Pruning + Knowledge Distillation
- **Recovery Strategy**: Supervised finetuning and hidden-state distillation for moderate compression (≤40%)
- **High Compression**: Widthwise pruning + knowledge distillation for ≥40% compression
- **Training Efficiency**: LoRA adapters reduce GPU hours by 70%
- **Performance**: Maintains over 95% original performance

### 4. Efficient Model Architectures

#### EdgeVLA 
- **Inference Speedup**: 7x faster inference
- **Method**: Eliminates autoregressive requirement for end-effector prediction
- **Architecture**: Uses Small Language Models (SLMs)
- **Performance**: Comparable training characteristics to OpenVLA with better efficiency

#### SmolVLA (Mid-2025)
- **Parameter Count**: ~450M parameters (compact design)
- **Training**: Single GPU trainable
- **Deployment**: CPU-deployable with competitive performance

## Benchmark Performance Summary

### LIBERO Benchmark Results
- **BitVLA**: Matches OpenVLA-OFT performance with 11x memory reduction
- **Compressor-VLA**: 97.1% success rate vs 97.3% baseline
- **Task Suites**: LIBERO-Spatial, LIBERO-Object, LIBERO-Goal, LIBERO-Long

### Real-World Performance
- **CoT-VLA**: +17% improvement in real-world tasks, +6% in simulations
- **Figure AI VLA**: Zero-shot generalization on thousands of novel objects
- **Commercial Deployment**: Tesla's ~1,000 Optimus prototypes (mid-2025)

## Hardware Deployment Specifications

### Edge Device Compatibility
- **BitVLA**: NVIDIA GeForce RTX 3050 Ti Laptop (4GB VRAM)
- **OpenVLA Quantized**: RTX 4090 for 4-bit inference
- **Memory Requirements**: As low as 1.4GB for BitVLA

### Commercial Applications
- **Figure AI**: Embedded low-power GPU deployment
- **Tesla**: Manufacturing operations with Optimus prototypes
- **Training Efficiency**: CogACT 7.6B parameter model optimized for edge deployment

## Compression Trade-off Analysis

### High Compression (>50% reduction)
- **Best Practice**: Widthwise pruning + knowledge distillation
- **Memory Savings**: Up to 11x (BitVLA)
- **Performance Impact**: <5% degradation when properly implemented

### Moderate Compression (20-40% reduction)
- **Best Practice**: Supervised finetuning + hidden-state distillation
- **Latency Gains**: 4-7x speedup possible
- **Accuracy Retention**: >95% of original performance

### Quantization-Specific Trade-offs
- **FP8**: Minimal accuracy loss, significant memory savings
- **4-bit**: Consumer GPU deployment enabled
- **1-bit (Ternary)**: 11x memory reduction with matched performance

## Research Gaps and Future Directions

### Identified Limitations
1. Limited long-term deployment studies on edge devices
2. Insufficient analysis of compression effects on different robot morphologies
3. Need for standardized benchmarks across compression techniques

### Emerging Trends
1. Multi-modal compression frameworks (vision + language + action)
2. Hardware-aware compression optimization
3. Dynamic compression based on task complexity
4. Federated learning integration with compressed VLA models

## Conclusion

Recent advances in VLA model compression demonstrate remarkable progress in making large-scale vision-language-action models deployable on edge devices. BitVLA's 1-bit quantization achieving 11x memory reduction while maintaining performance represents a breakthrough, while techniques like EdgeVLA's 7x inference speedup show the potential of architectural innovations. The field is rapidly moving toward practical deployment solutions that balance computational efficiency with task performance.

---

*Literature review conducted on April 5, 2026, covering papers from 2024-2026.*