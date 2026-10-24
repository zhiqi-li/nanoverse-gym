"""Retry anonymous, hash-checked LIBERO GETs without changing the native installer."""

RETRY_ARGUMENTS = ('--retry', '8', '--retry-all-errors', '--retry-max-time', '600',
                   '--connect-timeout', '15', '--max-time', '60')
TRANSPORT = {'kind': 'bounded-curl-get-retries.v1', 'arguments': list(RETRY_ARGUMENTS),
             'native_sha256_checks_unchanged': True}


class AssetDownloads:
    def __init__(self, installation):
        self.installation = installation

    def __getattr__(self, name):
        return getattr(self.installation, name)

    def run(self, *arguments, **kwargs):
        if arguments and arguments[0] == 'curl':
            arguments = ('curl', *RETRY_ARGUMENTS, *arguments[1:])
        return self.installation.run(*arguments, **kwargs)


def public_recipe(recipe):
    native_assets = recipe.assets

    def assets(installation):
        return native_assets(AssetDownloads(installation))

    recipe.assets = assets
    return recipe
