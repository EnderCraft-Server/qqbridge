@echo off
python -m venv qqbridge_env
qqbridge_env\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
copy .env.example .env