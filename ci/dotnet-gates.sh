#!/bin/sh
# Run the reproducible .NET checks used by both local developers and Woodpecker.
set -eu

# Resolve paths from the repository root even when invoked from another directory.
cd "$(dirname "$0")/.."
# Locked restore verifies the committed NuGet dependency graph.
dotnet restore HelloWorld.slnx --locked-mode
# Build once in Release. Subsequent steps reuse those dependencies and binaries.
dotnet build HelloWorld.slnx --configuration Release --no-restore
dotnet test HelloWorld.slnx --configuration Release --no-build
# Verify formatting without rewriting any files.
dotnet format HelloWorld.slnx --verify-no-changes --no-restore
