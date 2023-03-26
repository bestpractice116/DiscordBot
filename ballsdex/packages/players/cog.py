import discord
import logging
import enum

from typing import TYPE_CHECKING
from collections import defaultdict
from tortoise.exceptions import DoesNotExist

from discord import app_commands
from discord.ui import View, Button, button
from discord.ext import commands

from ballsdex.core.models import Ball, Player, BallInstance, DonationPolicy
from ballsdex.core.utils.transformers import BallInstanceTransform
from ballsdex.packages.players.countryballs_paginator import CountryballsViewer

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.countryballs")


class DonationRequest(View):
    def __init__(
        self, interaction: discord.Interaction, countryball: BallInstance, new_player: Player
    ):
        super().__init__(timeout=120)
        self.original_interaction = interaction
        self.countryball = countryball
        self.new_player = new_player

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id != self.new_player.discord_id:
            await interaction.response.send_message(
                "You are not allowed to interact with this menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        await self.original_interaction.followup.edit_message("@original", view=self)

    @button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept(self, interaction: discord.Interaction, button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True
        self.countryball.trade_player = self.countryball.player
        self.countryball.player = self.new_player
        await self.countryball.save()
        await interaction.response.edit_message(
            content=interaction.message.content
            + "\n\N{WHITE HEAVY CHECK MARK} The donation was accepted!",
            view=self,
        )

    @button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny(self, interaction: discord.Interaction, button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=interaction.message.content + "\n\N{CROSS MARK} The donation was denied.",
            view=self,
        )


class SortingChoices(enum.Enum):
    alphabetic = "ball__country"
    catch_date = "-catch_date"
    rarity = "ball__rarity"
    special = "special__id"

    # manual sorts are not sorted by SQL queries but by our code
    # this may be do-able with SQL still, but I don't have much experience ngl
    duplicates = "manualsort-duplicates"


class Players(commands.GroupCog, group_name="balls"):
    """
    View and manage your countryballs collection.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @app_commands.command()
    @app_commands.describe(
        user="View someone else's collection",
        sort="Modify the default sorting of your countryballs",
    )
    async def list(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        sort: SortingChoices | None = None,
    ):
        """
        List your countryballs.
        """
        user: discord.User | discord.Member = user or interaction.user

        try:
            player = await Player.get(discord_id=user.id)
        except DoesNotExist:
            if user == interaction.user:
                await interaction.response.send_message("You don't have any countryball yet.")
            else:
                await interaction.response.send_message(
                    f"{user.name} doesn't have any countryball yet."
                )
            return

        await player.fetch_related("balls")
        if sort:
            if sort == SortingChoices.duplicates:
                countryballs = await player.balls.all().prefetch_related("ball")
                count = defaultdict(int)
                for countryball in countryballs:
                    count[countryball.ball.pk] += 1
                countryballs.sort(key=lambda m: (-count[m.ball.pk], m.ball.pk))
            else:
                countryballs = (
                    await player.balls.all().prefetch_related("ball").order_by(sort.value)
                )
        else:
            countryballs = (
                await player.balls.all().prefetch_related("ball").order_by("-favorite", "-shiny")
            )

        if len(countryballs) < 1:
            if user == interaction.user:
                await interaction.response.send_message("You don't have any countryball yet.")
            else:
                await interaction.response.send_message(
                    f"{user.name} doesn't have any countryball yet."
                )
            return

        paginator = CountryballsViewer(interaction, countryballs)
        if user == interaction.user:
            await paginator.start()
        else:
            await paginator.start(content=f"Viewing {user.name}'s countryballs")

    @app_commands.command()
    async def completion(self, interaction: discord.Interaction):
        """
        Show your current completion of the BallsDex.
        """
        # Filter disabled balls, they do not count towards progression
        # Only ID and emoji is interesting for us
        bot_countryballs = {
            x.pk: x.emoji_id for x in await Ball.filter(enabled=True).only("id", "emoji_id")
        }
        # Set of ball IDs owned by the player
        owned_countryballs = set(
            x[0]
            for x in await BallInstance.filter(player__discord_id=interaction.user.id)
            .distinct()  # Do not query everything
            .values_list("ball_id")
        )

        embed = discord.Embed(
            description="BallsDex progression: "
            f"**{round(len(owned_countryballs)/len(bot_countryballs)*100, 4)}%**",
            colour=discord.Colour.blurple(),
        )
        embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )

        def fill_fields(title: str, emoji_ids: set[int]):
            # check if we need to add "(continued)" to the field name
            first_field_added = False
            buffer = ""

            for emoji_id in emoji_ids:
                emoji = self.bot.get_emoji(emoji_id)
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        embed.add_field(name="\u200B", value=buffer, inline=False)
                    else:
                        embed.add_field(name=f"__**{title}**__", value=buffer, inline=False)
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    embed.add_field(name="\u200B", value=buffer, inline=False)
                else:
                    embed.add_field(name=f"__**{title}**__", value=buffer, inline=False)

        if owned_countryballs:
            # Getting the list of emoji IDs from the IDs of the owned countryballs
            fill_fields("Owned countryballs", set(bot_countryballs[x] for x in owned_countryballs))
        else:
            embed.add_field(name="__**Owned countryballs**__", value="Nothing yet.", inline=False)

        if missing := set(y for x, y in bot_countryballs.items() if x not in owned_countryballs):
            fill_fields("Missing countryballs", missing)
        else:
            embed.add_field(
                name="__**:tada: No missing countryball, congratulations! :tada:**__",
                value="\u200B",
                inline=False,
            )  # force empty field value

        await interaction.response.send_message(embed=embed)

    @app_commands.command()
    @app_commands.describe(countryball="The countryball you want to inspect")
    async def info(
        self,
        interaction: discord.Interaction,
        countryball: BallInstanceTransform,
    ):
        """
        Display info from a specific countryball.
        """
        if not countryball:
            return
        await interaction.response.defer(thinking=True)
        content, file = await countryball.prepare_for_message(interaction)
        await interaction.followup.send(content=content, file=file)
        file.close()

    @app_commands.command()
    async def last(self, interaction: discord.Interaction):
        """
        Display info of your last caught countryball.
        """
        await interaction.response.defer(thinking=True)
        try:
            player = await Player.get(discord_id=interaction.user.id)
        except DoesNotExist:
            await interaction.followup.send("You do not have any countryball yet.", ephemeral=True)
            return

        countryball = await player.balls.all().order_by("-id").first().select_related("ball")
        if not countryball:
            await interaction.followup.send("You do not have any countryball yet.", ephemeral=True)
            return

        content, file = await countryball.prepare_for_message(interaction)
        await interaction.followup.send(content=content, file=file)
        file.close()

    @app_commands.command()
    @app_commands.describe(countryball="The countryball you want to set/unset as favorite")
    async def favorite(
        self,
        interaction: discord.Interaction,
        countryball: BallInstanceTransform,
    ):
        """
        Set favorite countryballs.
        """
        if not countryball.favorite:

            player = await Player.get(discord_id=interaction.user.id).prefetch_related("balls")
            if await player.balls.filter(favorite=True).count() > 20:
                await interaction.response.send_message(
                    "You cannot set more than 20 favorite countryballs.", ephemeral=True
                )
                return

            countryball.favorite = True  # type: ignore
            await countryball.save()
            emoji = self.bot.get_emoji(countryball.ball.emoji_id) or ""
            await interaction.response.send_message(
                f"{emoji} `#{countryball.pk:0X}` {countryball.ball.country} "
                "is now a favorite countryball!",
                ephemeral=True,
            )

        else:
            countryball.favorite = False  # type: ignore
            await countryball.save()
            emoji = self.bot.get_emoji(countryball.ball.emoji_id) or ""
            await interaction.response.send_message(
                f"{emoji} `#{countryball.pk:0X}` {countryball.ball.country} "
                "isn't a favorite countryball anymore.",
                ephemeral=True,
            )

    @app_commands.command()
    @app_commands.choices(
        policy=[
            app_commands.Choice(name="Accept all donations", value=DonationPolicy.ALWAYS_ACCEPT),
            app_commands.Choice(
                name="Request your approval first", value=DonationPolicy.REQUEST_APPROVAL
            ),
            app_commands.Choice(name="Deny all donations", value=DonationPolicy.ALWAYS_DENY),
        ]
    )
    async def donation_policy(
        self, interaction: discord.Interaction, policy: app_commands.Choice[int]
    ):
        """
        Chance how you want to receive donations from /balls give

        policy: DonationPolicy
            The new policy for accepting donations
        """
        player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player.donation_policy = policy.value
        if policy.value == DonationPolicy.ALWAYS_ACCEPT:
            await interaction.response.send_message(
                "Setting updated, you will now receive all donated countryballs immediately."
            )
        elif policy.value == DonationPolicy.REQUEST_APPROVAL:
            await interaction.response.send_message(
                "Setting updated, you will now have to approve donation requests manually."
            )
        elif policy.value == DonationPolicy.ALWAYS_DENY:
            await interaction.response.send_message(
                f"Setting updated, it is now impossible to use {self.give.extras['mention']} with "
                "you. It is still possible to perform donations using the trade system."
            )
        else:
            await interaction.response.send_message("Invalid input!")
            return
        await player.save()  # do not save if the input is invalid

    @app_commands.command()
    async def give(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        countryball: BallInstanceTransform,
    ):
        """
        Give a countryball to a user.

        Parameters
        ----------
        user: discord.User
            The user you want to give a countryball to
        countryball: BallInstance
            The countryball you're giving away
        """
        if not countryball:
            return
        if user.bot:
            await interaction.response.send_message("You cannot donate to bots.")
            return

        new_player, _ = await Player.get_or_create(discord_id=user.id)
        old_player = countryball.player

        if new_player == old_player:
            await interaction.response.send_message("You cannot give a ball to yourself.")
            return
        if new_player.donation_policy == DonationPolicy.ALWAYS_DENY:
            await interaction.response.send_message(
                "This player does not accept donations. You can use trades instead."
            )
            return
        elif new_player.donation_policy == DonationPolicy.REQUEST_APPROVAL:
            await interaction.response.send_message(
                f"Hey {user.mention}, {interaction.user.name} wants to give you "
                f"{countryball.description(include_emoji=True, bot=interaction.client)}!\n"
                "Do you accept this donation?",
                view=DonationRequest(interaction, countryball, new_player),
            )

        countryball.player = new_player
        countryball.trade_player = old_player
        await countryball.save()

        await interaction.response.send_message(
            "You just gave the countryball "
            f"{countryball.description(short=True, include_emoji=True, bot=self.bot)} to "
            f"{user.mention}!"
        )
