"""Use upstream RoboCasa downloads with the accepted asset hashes and members."""

from pathlib import Path

from portable.native_inventory import digest


def public_recipe(recipe):
    from nanoverse.resources import fetch

    recipe.ASSETS = Path(__file__).with_suffix('.json')
    recipe.SPEC = {**recipe.SPEC, 'assets_sha256': digest(recipe.ASSETS)}
    native_preflight = recipe.preflight

    def preflight(ctx):
        # The frozen owner's dependency guard assumes S3. Populate its verified
        # cache from HTTPS first, then keep its machine and renderer checks.
        for pack in recipe.packs():
            fetch(pack['resource'], ctx.root / 'cache/resources')
        native_preflight(ctx)

    recipe.preflight = preflight
    return recipe
