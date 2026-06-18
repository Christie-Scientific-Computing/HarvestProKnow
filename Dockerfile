FROM python:3.14


RUN apt-get update && apt-get upgrade -Y
RUN apt-get install curl

RUN curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
RUN curl https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list | tee /etc/apt/sources.list.d/mssql-release.list

RUN apt-get update
RUN ACCEPT_EULA=Y apt-get install -y msodbcsql17

RUN apt install freetds-dev

RUN mkdir logs
RUN pip install -r requirements.txt