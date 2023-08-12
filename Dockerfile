FROM lscr.io/linuxserver/ffmpeg:latest

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

WORKDIR /app

COPY ./app/req.txt ./

RUN pip install -r req.txt

COPY ./app ./

ENTRYPOINT ["python3"]

CMD ["main.py"]
