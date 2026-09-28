from setuptools import setup, find_packages

setup(
    name="platform_receiver",
    version="1.0.0",
    description="Shared base classes and utilities for WaddleBot platform receiver bots",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "httpx>=0.27.0",
    ],
)
