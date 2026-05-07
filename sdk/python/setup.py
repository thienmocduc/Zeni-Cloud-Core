"""Setup for zenicloud Python SDK."""
from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="zenicloud",
    version="1.0.0",
    description="Official Python SDK for Zeni Cloud — Cloud OS thống nhất cho doanh nghiệp Việt Nam (100% GCP)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Zeni Holdings",
    author_email="caotuanphat581@gmail.com",
    url="https://zenicloud.io",
    project_urls={
        "Documentation": "https://zenicloud.io/docs",
        "Source": "https://github.com/zenicloud/sdk-python",
        "Bug Tracker": "https://github.com/zenicloud/sdk-python/issues",
    },
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "httpx>=0.25.0",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    keywords="zenicloud cloud-os ai-design imagen-3 gemini vietnam",
)
