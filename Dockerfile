FROM lscr.io/linuxserver/ffmpeg:latest

RUN apt-get update && apt-get install -y \
    wget \
    python3 \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

RUN wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

COPY ./app/req.txt ./

RUN pip install -r req.txt

COPY ./app ./

ENTRYPOINT ["python3"]

CMD ["main.py"]
