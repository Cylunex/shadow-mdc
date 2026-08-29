import uvicorn


def run() -> None:
    uvicorn.run("shadow_mdc.api:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
