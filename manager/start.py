import os
from pathlib import Path

import uvicorn


if __name__ == '__main__':
    directories = ['/data', '/data/chatgpt2api', '/data/chatgpt2api/images', '/config']
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)
        if os.getuid() == 0:
            os.chown(directory, 101, 101)
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(101)
        os.setuid(101)
    uvicorn.run('app:app', host='0.0.0.0', port=8000)
