FROM lscr.io/linuxserver/ffmpeg:latest

ARG USER_ID=1000
ARG GROUP_ID=1000

RUN groupmod -g 3000 abc
RUN addgroup --gid $GROUP_ID lestar
RUN adduser --disabled-password --gecos '' --uid $USER_ID --gid $GROUP_ID lestar

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

RUN chown -R lestar:lestar /app

ENTRYPOINT ["python3"]

CMD ["main.py"]
