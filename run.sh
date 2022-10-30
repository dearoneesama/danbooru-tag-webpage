#!/bin/bash

PORT=29432

#fuser -k $PORT/tcp
ps -ef | grep gunicorn | grep -v grep | awk '{print $2}' | xargs -r kill -9
./bin/activate
./bin/python3 -m gunicorn -w 1 -k uvicorn.workers.UvicornWorker --timeout 180 server:app -b 0.0.0.0:$PORT
#./bin/python3 -m uvicorn server:app --host 0.0.0.0 --port $PORT --reload
