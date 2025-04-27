#!/bin/bash
# Copyright 2024 IBM Corp.
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
set -e

# Initialize submodules
git submodule update --init

echo "Setting up tgpt-code-interpreter image"
# Try to pull the latest remote image and tag locally, or build if unavailable
if docker pull ghcr.io/ucsd-ets/tgpt-code-interpreter:latest > /dev/null 2>&1; then
  echo "Pulled remote interpreter image, tagging for local use"
  docker tag ghcr.io/ucsd-ets/tgpt-code-interpreter:latest localhost/tgpt-code-interpreter:local
else
  echo "Remote interpreter image not found, building locally"
  docker build -t localhost/tgpt-code-interpreter:local .
fi

echo "Setting up tgpt-code-executor image"
# Try to pull the latest remote executor image and tag locally, or build if unavailable
if docker pull ghcr.io/ucsd-ets/tgpt-code-executor:latest > /dev/null 2>&1; then
  echo "Pulled remote executor image, tagging for local use"
  docker tag ghcr.io/ucsd-ets/tgpt-code-executor:latest localhost/tgpt-code-executor:local
else
  echo "Remote executor image not found, building locally"
  docker build -t localhost/tgpt-code-executor:local executor
fi

# Clean up any existing local deployments
kubectl delete -f k8s/local.yaml || true
kubectl delete -f k8s/pull.yaml || true

# Deploy local manifests
kubectl apply -f k8s/local.yaml

# Wait for service pod to be ready
kubectl wait --for=condition=Ready pod/code-interpreter-service

# Forward ports for local testing
kubectl port-forward pods/code-interpreter-service 50081:50081 50051:50051 &

# Stream logs
kubectl logs --follow code-interpreter-service

# Wait for background processes
wait
