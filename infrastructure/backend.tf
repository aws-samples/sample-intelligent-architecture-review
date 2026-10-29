# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

###############################################################################
# Remote state backend (S3)
#
# Configured with PARTIAL config: the block is intentionally empty here so
# the same root module can target any account/bucket/key. Real values are
# supplied at init time via `-backend-config`, e.g.:
#
#   terraform init \
#     -backend-config=backend-config/dev.s3.tfbackend
#
# or inline:
#
#   terraform init \
#     -backend-config="bucket=<proj>-<env>-tfstate-<account>" \
#     -backend-config="key=<env>/terraform.tfstate" \
#     -backend-config="region=us-east-1"
#
# deploy.sh does this for you (it also creates the state bucket if absent).
# Same account + different env  -> different key  -> isolated state.
# Different account             -> different bucket -> isolated state.
###############################################################################

terraform {
  backend "s3" {
    encrypt = true
  }
}
