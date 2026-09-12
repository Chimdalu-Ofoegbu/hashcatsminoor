# make cpu_worker      CPU worker (any machine with g++)
# make worker          CUDA worker; ARCH defaults to the first GPU's compute capability
CXX ?= g++
NVCC ?= nvcc
ARCH ?= $(shell nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d '. ')
CXXFLAGS ?= -O2 -std=c++17 -Wall

cpu_worker: cpu_worker.cpp keccak.h worker_common.h
	$(CXX) $(CXXFLAGS) -o $@ cpu_worker.cpp

worker: worker.cu keccak.h worker_common.h
	@test -n "$(ARCH)" || (echo "set ARCH, e.g. make worker ARCH=120 for an RTX 5090" && exit 1)
	$(NVCC) -O3 -std=c++17 -arch=sm_$(ARCH) -o $@ worker.cu

clean:
	rm -f cpu_worker worker

.PHONY: clean
