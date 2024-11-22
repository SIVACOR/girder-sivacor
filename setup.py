from setuptools import setup, find_packages

with open('README.md') as readme_file:
    readme = readme_file.read()

requirements = [
    'girder>=5.0.0a1'
]

setup(
    author='Kacper Kowalik',
    author_email='xarthisius.kk@gmail.com',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
    ],
    description='Girder Plugin implementing SIVACOR',
    install_requires=requirements,
    license='Apache Software License 2.0',
    long_description=readme,
    long_description_content_type='text/x-rst',
    include_package_data=True,
    keywords='girder-plugin, girder_sivacor',
    name='girder_sivacor',
    packages=find_packages(exclude=['test', 'test.*']),
    url='https://github.com/SIVACOR/girder-sivacor',
    version='0.1.0',
    zip_safe=False,
    entry_points={
        'girder.plugin': [
            'girder_sivacor = girder_sivacor:GirderPlugin'
        ]
    }
)
