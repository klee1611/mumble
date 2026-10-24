#!/bin/bash
set -e
virtualenv --without-pip virtualenv
pip install --require-hashes --no-deps -r requirements.txt --target virtualenv/lib/python3.11/site-packages
