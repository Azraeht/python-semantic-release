"""CLI
"""
import os
import sys

import click
import ndebug

from semantic_release import ci_checks
from semantic_release.errors import GitError, ImproperConfigurationError

from .history import (evaluate_version_bump, get_current_version, get_new_version,
                      get_previous_version, set_new_version)
from .history.logs import generate_changelog, markdown_changelog
from .hvcs import check_build_status, check_token, post_changelog
from .pypi import upload_to_pypi
from .settings import config
from .vcs_helpers import (checkout, commit_new_version, get_current_head_hash,
                          get_repository_owner_and_name, push_new_version, tag_new_version)

debug = ndebug.create(__name__)

COMMON_OPTIONS = [
    click.option('--major', 'force_level', flag_value='major', help='Force major version.'),
    click.option('--minor', 'force_level', flag_value='minor', help='Force minor version.'),
    click.option('--patch', 'force_level', flag_value='patch', help='Force patch version.'),
    click.option('--post', is_flag=True, help='Post changelog.'),
    click.option('--retry', is_flag=True, help='Retry the same release, do not bump.'),
    click.option('--noop', is_flag=True,
                 help='No-operations mode, finds the new version number without changing it.')
]


def common_options(func):
    """
    Decorator that adds all the options in COMMON_OPTIONS
    """
    for option in reversed(COMMON_OPTIONS):
        func = option(func)
    return func


def version(**kwargs):
    """
    Detects the new version according to git log and semver. Writes the new version
    number and commits it, unless the noop-option is True.
    """
    retry = kwargs.get("retry")
    if retry:
        click.echo('Retrying publication of the same version...')
    else:
        click.echo('Creating new version..')

    try:
        current_version = get_current_version()
    except GitError as e:
        click.echo(click.style(str(e), 'red'), err=True)
        return False

    click.echo('Current version: {0}'.format(current_version))
    level_bump = evaluate_version_bump(current_version, kwargs['force_level'])
    new_version = get_new_version(current_version, level_bump)

    if new_version == current_version and not retry:
        click.echo(click.style('No release will be made.', fg='yellow'))
        return False

    if kwargs['noop'] is True:
        click.echo('{0} Should have bumped from {1} to {2}.'.format(
            click.style('No operation mode.', fg='yellow'),
            current_version,
            new_version
        ))
        return False

    if config.getboolean('semantic_release', 'check_build_status'):
        click.echo('Checking build status..')
        owner, name = get_repository_owner_and_name()
        if not check_build_status(owner, name, get_current_head_hash()):
            click.echo(click.style('The build has failed', 'red'))
            return False
        click.echo(click.style('The build was a success, continuing the release', 'green'))

    if retry:
        # No need to make changes to the repo, we're just retrying.
        return True

    set_new_version(new_version)
    if config.get('semantic_release', 'version_source') == 'commit':
        commit_new_version(new_version)
    tag_new_version(new_version)
    click.echo('Bumping with a {0} version to {1}.'.format(level_bump, new_version))
    return True


def changelog(**kwargs):
    """
    Generates the changelog since the last release.
    :raises ImproperConfigurationError: if there is no current version
    """
    current_version = get_current_version()
    debug('changelog got current_version', current_version)

    if current_version is None:
        raise ImproperConfigurationError(
            "Unable to get the current version. "
            "Make sure semantic_release.version_variable "
            "is setup correctly"
        )
    previous_version = get_previous_version(current_version)
    debug('changelog got previous_version', previous_version)

    if kwargs['unreleased']:
        log = generate_changelog(current_version, None)
    else:
        log = generate_changelog(previous_version, current_version)
    click.echo(markdown_changelog(current_version, log, header=False))

    debug('noop={}, post={}'.format(kwargs.get('noop'), kwargs.get('post')))
    if not kwargs.get('noop') and kwargs.get('post'):
        if check_token():
            owner, name = get_repository_owner_and_name()
            click.echo('Updating changelog')
            post_changelog(
                owner,
                name,
                current_version,
                markdown_changelog(current_version, log, header=False)
            )
        else:
            click.echo(
                click.style('Missing token: cannot post changelog', 'red'), err=True)


def publish(**kwargs):
    """
    Runs the version task before pushing to git and uploading to pypi.
    """

    current_version = get_current_version()
    click.echo('Current version: {0}'.format(current_version))
    retry = kwargs.get("retry")
    debug('publish: retry=', retry)
    if retry:
        # The "new" version will actually be the current version, and the
        # "current" version will be the previous version.
        new_version = current_version
        current_version = get_previous_version(current_version)
    else:
        level_bump = evaluate_version_bump(current_version, kwargs['force_level'])
        new_version = get_new_version(current_version, level_bump)
    owner, name = get_repository_owner_and_name()

    branch = config.get('semantic_release', 'branch')
    debug('branch=', branch)
    ci_checks.check(branch)
    checkout(branch)

    if version(**kwargs):
        push_new_version(
            gh_token=os.environ.get('GH_TOKEN'),
            owner=owner,
            name=name,
            branch=branch
        )

        if config.getboolean('semantic_release', 'upload_to_pypi'):
            upload_to_pypi(
                username=os.environ.get('PYPI_USERNAME'),
                password=os.environ.get('PYPI_PASSWORD'),
                # We are retrying, so we don't want errors for files that are already on PyPI.
                skip_existing=retry,
                remove_dist=config.getboolean('semantic_release', 'remove_dist'),
                path=config.get('semantic_release', 'dist_path'),
            )

        if check_token():
            click.echo('Updating changelog')
            try:
                log = generate_changelog(current_version, new_version)
                post_changelog(
                    owner,
                    name,
                    new_version,
                    markdown_changelog(new_version, log, header=False)
                )
            except GitError:
                click.echo(click.style('Posting changelog failed.', 'red'), err=True)

        else:
            click.echo(
                click.style('Missing token: cannot post changelog', 'red'), err=True)

        click.echo(click.style('New release published', 'green'))
    else:
        click.echo('Version failed, no release will be published.', err=True)


def filter_output_for_secrets(message):
    output = message
    username = os.environ.get('PYPI_USERNAME')
    password = os.environ.get('PYPI_PASSWORD')
    gh_token = os.environ.get('GH_TOKEN')
    if username != '' and username is not None:
        output = output.replace(username, '$PYPI_USERNAME')
    if password != '' and password is not None:
        output = output.replace(password, '$PYPI_PASSWORD')
    if gh_token != '' and gh_token is not None:
        output = output.replace(gh_token, '$GH_TOKEN')

    return output

#
# Making the CLI commands.
# We have a level of indirection to the logical commands
# so we can successfully mock them during testing
#


@click.group()
@common_options
def main(**kwargs):
    if debug.enabled:
        debug('main args:', kwargs)
        message = ''
        for key in ['GH_TOKEN', 'PYPI_USERNAME', 'PYPI_PASSWORD']:
            message += '{}="{}",'.format(key, os.environ.get(key))
        debug('main env:', filter_output_for_secrets(message))

        obj = {}
        for key in ['check_build_status', 'commit_message', 'commit_parser', 'patch_without_tag',
                    'upload_to_pypi', 'version_source']:
            val = config.get('semantic_release', key)
            obj[key] = val
        debug('main config:', obj)


@main.command(name='publish', help=publish.__doc__)
@common_options
def cmd_publish(**kwargs):
    try:
        return publish(**kwargs)
    except Exception as error:
        click.echo(click.style(filter_output_for_secrets(str(error)), 'red'), err=True)
        exit(1)


@main.command(name='changelog', help=changelog.__doc__)
@common_options
@click.option(
    '--unreleased/--released',
    help="Decides whether to show the released or unreleased changelog."
)
def cmd_changelog(**kwargs):
    try:
        return changelog(**kwargs)
    except Exception as error:
        click.echo(click.style(filter_output_for_secrets(str(error)), 'red'), err=True)
        exit(1)


@main.command(name='version', help=version.__doc__)
@common_options
def cmd_version(**kwargs):
    try:
        return version(**kwargs)
    except Exception as error:
        click.echo(click.style(filter_output_for_secrets(str(error)), 'red'), err=True)
        exit(1)


if __name__ == '__main__':
    #
    # Allow options to come BEFORE commands,
    # we simply sort them behind the command instead.
    #
    # This will have to be removed if there are ever global options
    # that are not valid for a subcommand.
    #
    ARGS = sorted(sys.argv[1:], key=lambda x: 1 if x.startswith('--') else -1)
    main(args=ARGS)
