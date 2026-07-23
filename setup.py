from setuptools import setup, find_packages

with open("README.md") as readme_file:
    readme = readme_file.read()

girder_version = "5.0.12"
requirements = [
    "cryptography",
    f"girder>={girder_version}",
    "girder-async-routes>=0.1.3",
    f"girder-oauth>={girder_version}",
    f"girder-jobs>={girder_version}",
    f"girder-plugin-worker>={girder_version}",
    f"girder-user-quota>={girder_version}",
    "pandas",
    "pathspec",
    "pylibacl",
    "py-cpuinfo",
    "randomname",
    "tro-utils>=0.4.5",
]

setup(
    author="Kacper Kowalik",
    author_email="xarthisius.kk@gmail.com",
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
    ],
    description="Girder Plugin implementing SIVACOR",
    install_requires=requirements,
    license="Apache Software License 2.0",
    long_description=readme,
    long_description_content_type="text/x-rst",
    include_package_data=True,
    keywords="girder-plugin",
    name="girder-sivacor",
    packages=find_packages(exclude=["test", "test.*"]),
    url="https://github.com/SIVACOR/girder-sivacor",
    version="0.1.3",
    zip_safe=False,
    entry_points={
        "girder.plugin": ["sivacor = girder_sivacor:SIVACORPlugin"],
        "girder_worker_plugins": [
            "sivacor = girder_sivacor.worker_plugin:SIVACORWorkerPlugin"
        ],
    },
)
