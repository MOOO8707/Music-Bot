FROM python:3

RUN mkdir app
WORKDIR /app

COPY . .

RUN apt-get updata
RUN apt-get upgrade -y
RUN apt-get install ffmpeg

RUN pip3 install youtube_dl
RUN pip3 install discord
RUN pip3 install requests
RUN pip3 install bs4
RUN pip3 install psutil
RUN pip3 install PyNaCl

CMD [ "python3", "main.py" ]