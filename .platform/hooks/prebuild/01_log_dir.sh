#!/usr/bin/env bash
# Session logs are research data. Beanstalk replaces /var/app/current on every
# deploy, so keep them outside it. The Procfile points LOG_DIR here.
# Note: this only survives deploys, not instance replacement. Copy logs off the
# instance (e.g. to S3) before terminating or rebuilding the environment.
set -euo pipefail

LOG_DIR=/var/app/data/logs

mkdir -p "$LOG_DIR"
chown -R webapp:webapp /var/app/data
