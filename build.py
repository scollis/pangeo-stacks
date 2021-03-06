#!/usr/bin/env python3
"""
Build an image in the pangeo stack.
"""
import subprocess
from dateutil.parser import parse
from datetime import datetime
import pytz
import docker
import os
from functools import lru_cache
from repo2docker.app import Repo2Docker
import argparse

def modified_date(n, *paths, **kwargs):
    """
    Return the commit date for nth commit that modified *paths
    """
    iso_date = subprocess.check_output([
        'git',
        'log',
        '-n', f'{n}',
        '--pretty=format:%cd',
        '--date=iso',
        '--',
        *paths
    ], **kwargs).decode('utf-8').strip().split('\n')[-1]
    return parse(iso_date)


@lru_cache(128)
def image_exists_in_registry(client, image_spec):
    """
    Return true if image exists in docker registry
    """
    try:
        image_manifest = client.images.get_registry_data(image_spec)
        return image_manifest is not None
    except docker.errors.ImageNotFound:
        return False
    except docker.errors.APIError as e:
        # This message seems to vary across registries?
        if e.explanation.startswith('manifest unknown: '):
            return False
        else:
            raise

def docker_build(image_spec, path, build_args):
    print(f'Building {image_spec}')
    if os.path.exists(os.path.join(path, 'Dockerfile')):
        df_path = os.path.join(path, 'Dockerfile')
    else:
        df_path = os.path.join(path, 'binder', 'Dockerfile')
    command = [
        'docker', 'build',
        '-t', image_spec,
        '-f', df_path
    ]
    for k, v in build_args.items():
        command += ['--build-arg', f'{k}={v}']
    command.append(path)
    subprocess.check_call(command)


def r2d_build(image, image_spec, cache_from):
    r2d = Repo2Docker()

    r2d.subdir = image
    r2d.output_image_spec = image_spec
    r2d.user_id = 1000
    r2d.user_name = 'jovyan'
    r2d.cache_from = cache_from

    r2d.initialize()
    r2d.build()

    if os.path.exists(os.path.join(r2d.subdir, 'binder/verify')):
        print(f'Validating {image_spec}')
        # Validate the built image
        subprocess.check_call([
            'docker',
            'run',
            '-i', '-t',
            f'{r2d.output_image_spec}',
            'binder/verify'
        ])
    else:
        print(f'No verify script found for {image_spec}')

def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument(
        'image',
        help='Image to build. Subdirectory with this name must exist'
    )
    argparser.add_argument(
        '--image-prefix',
        help='Prefix for image to be built. Usually contains registry url and name',
        default='pangeo/'
    )
    argparser.add_argument(
        '--push',
        help='Push the built image to the docker registry',
        action='store_true',
        default=False
    )

    args = argparser.parse_args()

    image_name = f'{args.image_prefix}{args.image}'
    print(f'Building {image_name}')
    client = docker.from_env()
    cache_from = []

    # Pull the most recent built image available for this docker image
    # We can re-use the cache from that, significantly speeding up
    # our image builds
    for i in range(1, 100):
        date = modified_date(i, '.')
        # Stick to UTC for calver
        existing_calver = date.astimezone(pytz.utc).strftime('%Y.%m.%d')
        existing_image_spec = f'{image_name}:{existing_calver}'
        if image_exists_in_registry(client, existing_image_spec):
            print(f'Re-using cache from {existing_image_spec}')
            cache_from = [existing_image_spec]
            subprocess.check_call([
                'docker',
                'pull', existing_image_spec
            ])
            break


    calver = datetime.utcnow().strftime('%Y.%m.%d')
    dockerfile_paths = [
        os.path.join(args.image, 'binder', 'Dockerfile'),
        os.path.join(args.image, 'Dockerfile')
    ]
    if any((os.path.exists(df) for df in dockerfile_paths)):
        # Use docker if we have a Dockerfile
        # Can be just r2d once we can pass arbitrary BUILD ARGs to it
        # https://github.com/jupyter/repo2docker/issues/645
        docker_build(
            f'{image_name}:{calver}',
            args.image,
            {
                'CALVER': calver
            }
        )
    else:
        # Build regular image
        r2d_build(
            args.image,
            f'{image_name}:{calver}',
            cache_from
        )

    # Build onbuild image
    docker_build(
        f'{image_name}-onbuild:{calver}',
        'onbuild',
        {'BASE_IMAGE_SPEC': f'{image_name}:{calver}'}
    )


if __name__ == '__main__':
    main()