#!/usr/bin/env bash
exec ssh -N -L 8000:127.0.0.1:8000 -L 5001:127.0.0.1:5001 server-4090
