"""配置 .env 文件"""

import re
import sys

if input("运行此文件会覆盖已有的 .env 文件！\n确认要继续吗？[y/N]") != 'y':
    print("程序终止。")
    sys.exit(1)
print("注意，部分复杂内容默认不提供配置，请自己更改。")
env_example_file = open(".env.example", "r", encoding="utf-8")
env_file = open(".env", "w", encoding="utf-8")
env_example_lines = env_example_file.readlines()
env_lines = list()
for line in env_example_lines:
    line.replace("\n", "")
    print(line)
    if line[0] == "#": # 注释
        continue

    # 一行应该由输入配置的文字的格式应该为：大写字母和下划线组成的配置项目 + '=' + '<' + 配置说明 + '>'
    # [A-Z]
    if re.match(".+=<.*>", line):
        equal_index = line.find("=")
        config_name = line[:equal_index]
        config_help = line[equal_index+1:].replace('<','').replace('>','')
        print(f"可配置项目：{config_name}：{config_help}")
        config_value = input(f"输入配置值：")
        env_lines.append(f"{config_name}={config_value}")
    else:
        env_lines.append(line)
print("配置完成，正在写入：")
for line in env_lines:
    env_file.write(line)
env_file.close()
env_example_file.close()
input("配置流程结束，按 Enter 以退出")