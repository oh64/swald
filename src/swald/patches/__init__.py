from . import discord as discord_patch
from . import fluxer


def apply():
    fluxer.apply()
    discord_patch.apply()
