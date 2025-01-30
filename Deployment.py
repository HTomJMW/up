import os
import discord
from discord.ext import commands, tasks
import aiosqlite
import asyncio
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, time
from typing import List, Tuple, Union, Optional
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_fixed

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Időzónák definiálása
BOT_TIMEZONE = ZoneInfo("Europe/Paris")  # A bot futási időzónája
USER_TIMEZONE = ZoneInfo("Europe/Budapest")  # A felhasználók időzónája

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def db_execute(query: str, params: tuple = (), commit: bool = False) -> list:
	try:
		async with aiosqlite.connect("schedule.db") as conn:
			cursor = await conn.cursor()
			await cursor.execute(query, params)
			result = await cursor.fetchall()
			if commit:
				await conn.commit()
			return result
	except aiosqlite.Error as e:
		print(f"⛔ Adatbázis hiba: {str(e)}")
		if 'conn' in locals():
			await conn.rollback()
		raise
	except Exception as e:
		print(f"⚠️ Váratlan hiba az adatbázis művelet során: {str(e)}")
		raise

async def setup_database():
	await db_execute("""
	CREATE TABLE IF NOT EXISTS schedule (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		server_name TEXT,
		time_slot TEXT,
		participants TEXT,
		channel_id TEXT,
		message_id TEXT
	)""", commit=True)

def parse_time_slot(time_slot: str) -> Tuple[datetime, datetime]:
	try:
		start_str, end_str = time_slot.split("-")
		today = datetime.now(USER_TIMEZONE).strftime("%Y-%m-%d")  # Magyarországi dátum

		start = datetime.strptime(f"{today} {start_str.strip()}:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=USER_TIMEZONE)
		end = datetime.strptime(f"{today} {end_str.strip()}:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=USER_TIMEZONE)

		if end < start:
			end += timedelta(days=1)

		return start, end
	except ValueError:
		raise commands.BadArgument("❌ Hibás időformátum! Használd a **HH:MM-HH:MM** formátumot.")

def create_event_embed(server_name: str, time_slot: str, participants: List[str], end_time: datetime) -> discord.Embed:
	try:
		now = datetime.now(USER_TIMEZONE)  # Magyarországi idő szerint
		embed = discord.Embed(
			title=f"🪖 {server_name}",
			description="\u200b",
			color=discord.Color.red() if now >= end_time else discord.Color.green(),
		)

		current_time = now.strftime("%H:%M")
		current_date = now.strftime("%Y-%m-%d")

		time_status = "🟢 Lejár" if now < end_time else "🔴 Lejárt"
		embed.add_field(
			name="🕒 Időpont",
			value=(
				f"```{time_slot}```\n"
				f"{time_status}: <t:{int(end_time.timestamp())}:R>\n"
				f"`{current_date}`"
			),
			inline=True
		)

		participants_display = []
		for p in participants:
			try:
				parts = p.split(":", 2)
				name = parts[1]

				if len(parts) >= 3 and parts[2].isdigit():
					delay = int(parts[2])
					if delay > 0:
						name += f" (+{delay}⏰)"

				participants_display.append(f"```• {name}```")

			except Exception as e:
				print(f"Hiba résztvevő feldolgozásánál: {str(e)}")
				continue

		embed.add_field(
			name=f"👥 Résztvevők ({len(participants_display)})",
			value="".join(participants_display) if participants_display else "```Nincs még résztvevő```",
			inline=True
		)

		embed.set_footer(
		#    text="🟢 Aktív" if now < end_time else "🔴 Lejárt",
		#    icon_url="https://i.imgur.com/ej7GpoO.png"
		)

		return embed

	except Exception as e:
		print(f"⚠️ Hiba embed készítésénél: {str(e)}")
		raise

async def schedule_event_end(row_id: int, channel_id: int, server_name: str, time_slot: str) -> None:
	try:
		_, end_time = parse_time_slot(time_slot)
		now = datetime.now(USER_TIMEZONE)

		delay = (end_time - now).total_seconds()
		if delay > 0:
			await asyncio.sleep(delay)

		await db_execute("UPDATE schedule SET participants='' WHERE id=?", (row_id,), commit=True)

		channel = bot.get_channel(channel_id)
		if channel:
			await update_schedule_message(row_id, channel, server_name, time_slot, [])

		print(f"⏳ Esemény lezárva: {server_name} ({time_slot})")

	except aiosqlite.Error as e:
		print(f"⛔ Adatbázis hiba az esemény lezárásakor: {str(e)}")
		if channel:
			await channel.send(f"❌ Adatbázis hiba történt a(z) **{server_name}** esemény lezárásakor!")
	except discord.NotFound:
		print(f"❌ Csatorna vagy üzenet nem található (ID: {channel_id})")
	except discord.Forbidden:
		print(f"⛔ Nincs jogosultság a csatornához (ID: {channel_id})")
	except Exception as e:
		print(f"⚠️ Váratlan hiba az esemény lezárásakor: {str(e)}")
		if channel:
			await channel.send(f"❌ Váratlan hiba történt a(z) **{server_name}** esemény lezárásakor!")

def get_participants_list(participants: Union[str, List[str]]) -> List[str]:
	try:
		if isinstance(participants, str):
			return participants.split(", ") if participants else []
		return participants

	except Exception as e:
		print(f"⚠️ Hiba résztvevők feldolgozásánál: {str(e)}")
		return []

async def update_schedule_message(row_id: int,channel: discord.TextChannel,server_name: str,time_slot: str,participants: List[str],reset_flag: bool = False,) -> None:
	try:
		result = await db_execute("SELECT message_id FROM schedule WHERE id=?", (row_id,))
		message_id = result[0][0] if result and result[0][0] else None

		start_time, end_time = parse_time_slot(time_slot)
		now = datetime.now(USER_TIMEZONE)

		embed = create_event_embed(server_name, time_slot, participants, end_time)

		view = discord.ui.View(timeout=None)

		if now >= end_time and not reset_flag:
			view.add_item(EventEndedButton(row_id))
		else:
			if reset_flag:
				participants = []

			view.add_item(JoinButton(row_id, server_name, participants, time_slot))
			view.add_item(DelayButton(row_id, server_name, participants, time_slot))
			view.add_item(LeaveButton(row_id, server_name, participants, time_slot))

		try:
			if message_id:
				bot.add_view(view, message_id=int(message_id))
			else:
				bot.add_view(view)
		except Exception as e:
			print(f"⚠️ Hiba a view regisztrálásánál: {str(e)}")

		try:
			if message_id:
				message = await channel.fetch_message(int(message_id))
				await message.edit(embed=embed, view=view)
			else:
				new_message = await channel.send(embed=embed, view=view)
				await db_execute(
					"UPDATE schedule SET message_id=? WHERE id=?",
					(new_message.id, row_id),
					commit=True,
				)
		except discord.NotFound:
			print(f"❌ Üzenet nem található (ID: {message_id}), új üzenet küldése...")
			new_message = await channel.send(embed=embed, view=view)
			await db_execute(
				"UPDATE schedule SET message_id=? WHERE id=?",
				(new_message.id, row_id),
				commit=True,
			)
		except discord.Forbidden:
			print(f"⛔ Nincs jogosultság a csatornához (ID: {channel.id})")
		except Exception as e:
			print(f"⚠️ Kritikus hiba üzenet frissítésnél: {str(e)}")

	except Exception as e:
		print(f"⛔ Váratlan hiba az update_schedule_message függvényben: {str(e)}")
		raise

class JoinButton(discord.ui.Button):
	def __init__(self, row_id: int, server_name: str, participants: List[str], time_slot: str):
		super().__init__(
			label="✅ Csatlakozás",
			style=discord.ButtonStyle.success,
			custom_id=f"join_{row_id}"
		)
		self.row_id = row_id
		self.server_name = server_name
		self.participants = participants
		self.time_slot = time_slot

	async def callback(self, interaction: discord.Interaction):
		try:
			user_id = str(interaction.user.id)
			user_name = interaction.user.display_name
			result = await db_execute("SELECT participants FROM schedule WHERE id=?", (self.row_id,))
			current_participants = get_participants_list(result[0][0] if result else "")

			if not any(user_id in p for p in current_participants):
				current_participants.append(f"{user_id}:{user_name}")

			await db_execute(
				"UPDATE schedule SET participants=? WHERE id=?",
				(", ".join(current_participants), self.row_id),
				commit=True
			)

			await update_schedule_message(
				self.row_id,
				interaction.channel,
				self.server_name,
				self.time_slot,
				current_participants
			)

			await interaction.response.defer()
		except Exception as e:
			print(f"⚠️ Csatlakozási hiba: {str(e)}")
			await interaction.response.send_message("❌ Hiba történt a csatlakozás során!", ephemeral=True)

class DelayButton(discord.ui.Button):
	def __init__(self, row_id: int, server_name: str, participants: List[str], time_slot: str):
		super().__init__(
			label="⏰ Késés",
			style=discord.ButtonStyle.grey,
			custom_id=f"delay_{row_id}",
			emoji="🔄"
		)
		self.row_id = row_id
		self.server_name = server_name
		self.time_slot = time_slot
		self.participants = participants

	async def callback(self, interaction: discord.Interaction):
		user_id = str(interaction.user.id)
		result = await db_execute("SELECT participants FROM schedule WHERE id=?", (self.row_id,))
		current_participants = get_participants_list(result[0][0] if result else [])

		user_found = any(user_id in p for p in current_participants)

		if not user_found:
			await interaction.response.send_message(
				"❌ Csak akkor adhatsz hozzá késést, ha már csatlakoztál az eseményhez!",
				ephemeral=True
			)
			return

		new_participants = []
		for p in current_participants:
			parts = p.split(":")
			if parts[0] == user_id:
				current_delay = int(parts[2]) if len(parts) > 2 else 0
				# Logika: 0 → 1 → 2 → 0
				new_delay = (current_delay + 1) % 3
				new_participants.append(f"{parts[0]}:{parts[1]}:{new_delay}")
			else:
				new_participants.append(p)

		await db_execute(
			"UPDATE schedule SET participants=? WHERE id=?",
			(", ".join(new_participants), self.row_id),
			commit=True
		)

		await update_schedule_message(
			self.row_id,
			interaction.channel,
			self.server_name,
			self.time_slot,
			new_participants
		)

		await interaction.response.defer()

class LeaveButton(discord.ui.Button):
	def __init__(self, row_id: int, server_name: str, participants: List[str], time_slot: str):
		super().__init__(
			label="❌ Kilépés",
			style=discord.ButtonStyle.danger,
			custom_id=f"leave_{row_id}"
		)
		self.row_id = row_id
		self.server_name = server_name
		self.participants = participants
		self.time_slot = time_slot

	async def callback(self, interaction: discord.Interaction):
		user_id = str(interaction.user.id)
		result = await db_execute("SELECT participants FROM schedule WHERE id=?", (self.row_id,))
		current_participants = get_participants_list(result[0][0] if result else "")

		current_participants = [p for p in current_participants if user_id not in p]

		await db_execute(
			"UPDATE schedule SET participants=? WHERE id=?",
			(", ".join(current_participants), self.row_id),
			commit=True
		)

		await update_schedule_message(
			self.row_id,
			interaction.channel,
			self.server_name,
			self.time_slot,
			current_participants
		)

		await interaction.response.defer()

class EventEndedButton(discord.ui.Button):
	def __init__(self, row_id: int):
		super().__init__(
			label="⏳ Esemény lejárt",
			style=discord.ButtonStyle.secondary,
			disabled=True,
			custom_id=f"ended_{row_id}"
		)

@bot.command()
async def add_server(ctx, server_name: str, time_slot: str):
	try:
		try:
			start_time, end_time = parse_time_slot(time_slot)
		except commands.BadArgument as e:
			if message_sending_enabled:
				await ctx.send(f"❌ Hiba: {str(e)}", delete_after=10)
			return

		try:
			existing = await db_execute(
				"SELECT id FROM schedule WHERE server_name=? AND time_slot=?",
				(server_name, time_slot)
			)
		except aiosqlite.Error as e:
			print(f"⛔ Adatbázis hiba lekérdezésnél: {str(e)}")
			if message_sending_enabled:
				await ctx.send("🔴 Adatbázis hiba történt!", delete_after=10)
			return

		if existing:
			if message_sending_enabled:
				await ctx.send(
					f"⚠ A(z) `{server_name}` már létezik ebben az időpontban!",
					delete_after=15
				)
			return

		try:
			await db_execute(
				"INSERT INTO schedule (server_name, time_slot, participants, channel_id) VALUES (?, ?, '', ?)",
				(server_name, time_slot, ctx.channel.id),
				commit=True
			)
		except aiosqlite.Error as e:
			print(f"⛔ Adatbázis hiba beszúrásnál: {str(e)}")
			if message_sending_enabled:
				await ctx.send("🔴 Sikertelen mentés az adatbázisba!", delete_after=10)
			return

		try:
			row_id_result = await db_execute(
				"SELECT id FROM schedule WHERE server_name=? AND time_slot=?",
				(server_name, time_slot)
			)
			if not row_id_result:
				raise ValueError("Nincs visszaadott ID az adatbázisból")
			row_id = row_id_result[0][0]
		except (aiosqlite.Error, IndexError, ValueError) as e:
			print(f"⛔ Hiba ID lekérdezésnél: {str(e)}")
			if message_sending_enabled:
				await ctx.send("🔴 Hiba az esemény létrehozásakor!", delete_after=10)
			return

		try:
			channel = ctx.channel
			await update_schedule_message(row_id, channel, server_name, time_slot, [])
		except Exception as e:
			print(f"⛔ Hiba üzenet frissítésnél: {str(e)}")
			if message_sending_enabled:
				await ctx.send("🔴 Hiba az üzenet megjelenítésénél!", delete_after=10)
			return

		try:
			bot.loop.create_task(schedule_event_end(row_id, channel.id, server_name, time_slot))
		except Exception as e:
			print(f"⛔ Hiba task létrehozásánál: {str(e)}")
			if message_sending_enabled:
				await ctx.send("🔴 Hiba az esemény ütemezésénél!", delete_after=10)

		if message_sending_enabled:
			await show_schedule(ctx)

	finally:
		try:
			await ctx.message.delete(delay=2)
		except (discord.NotFound, discord.Forbidden):
			pass
		except Exception as e:
			print(f"⚠️ Hiba üzenet törlésénél: {str(e)}")

@bot.command()
async def remove_server(ctx, row_id: int):
	result = await db_execute("SELECT message_id, channel_id FROM schedule WHERE id=?", (row_id,))
	if not result:
		if message_sending_enabled:
			await ctx.send(f"❌ Nincs ilyen azonosítójú esemény: **{row_id}**")
		return

	message_id, channel_id = result[0]

	try:
		if message_id and channel_id:
			channel = bot.get_channel(int(channel_id))
			if channel:
				message = await channel.fetch_message(int(message_id))
				await message.delete()
	except discord.NotFound:
		print(f"Üzenet már törölve volt: {message_id}")
	except Exception as e:
		print(f"Hiba történt az üzenet törlésekor: {str(e)}")

	await db_execute("DELETE FROM schedule WHERE id=?", (row_id,), commit=True)
	await db_execute("VACUUM", commit=True)

	check = await db_execute("SELECT COUNT(*) FROM schedule WHERE id=?", (row_id,))
	count = check[0][0] if check else -1

	if count == 0:
		if message_sending_enabled:
			await ctx.send(f"✅ Esemény (ID: `{row_id}`) sikeresen törölve az adatbázisból!")
	else:
		if message_sending_enabled:
			await ctx.send(f"⚠ **Figyelem!** Az esemény törlése sikertelen volt! ({count} elem maradt)")

	await ctx.message.delete()

@bot.command()
async def show_schedule(ctx):
	records = await db_execute("SELECT id, server_name, time_slot, participants FROM schedule")

	if not records:
		return await ctx.send("Nincs aktív esemény.")

	for row in records:
		row_id, server_name, time_slot, participants = row
		await update_schedule_message(
			row_id,
			ctx.channel,
			server_name,
			time_slot,
			get_participants_list(participants)
		)

@bot.event
async def on_ready():
	try:
		print(f"{bot.user} elindult!")

		records = await db_execute("SELECT id, message_id, server_name, time_slot, participants FROM schedule")
		for row in records:
			row_id, message_id, server_name, time_slot, participants = row
			try:
				view = discord.ui.View(timeout=None)
				view.add_item(JoinButton(row_id, server_name, participants, time_slot))
				view.add_item(DelayButton(row_id, server_name, participants, time_slot))
				view.add_item(LeaveButton(row_id, server_name, participants, time_slot))
				bot.add_view(view, message_id=int(message_id))
			except Exception as e:
				print(f"Hiba a view regenerálásánál: {e}")

		await bot.wait_until_ready()
		if not schedule_reset_task.is_running():
			schedule_reset_task.start()
			print("⏳ Reset loop elindítva!")

	except Exception as e:
		print(f"⛔ Váratlan hiba az indításnál: {str(e)}")
		await bot.close()

message_sending_enabled = False

@bot.command()
@commands.is_owner()
async def message(ctx, mode: str):
	global message_sending_enabled

	if mode.lower() == "on":
		message_sending_enabled = True
		await ctx.send("✅ Az üzenetküldés engedélyezve lett!")
	elif mode.lower() == "off":
		message_sending_enabled = False
		await ctx.send("🔇 Az üzenetküldés le lett tiltva!")
	else:
		await ctx.send("❌ Hibás használat! Használat: `!message on` vagy `!message off`")

@bot.command()
@commands.is_owner()
async def reset_schedule(ctx):
	await schedule_reset_task()
	await ctx.send("🔄 **Schedule Reset manuálisan végrehajtva!**")

@bot.command()
@commands.is_owner()
async def reset_db(ctx):
	await db_execute("DELETE FROM schedule", commit=True)
	await db_execute("DELETE FROM sqlite_sequence WHERE name='schedule'", commit=True)
	await db_execute("VACUUM", commit=True)

	check = await db_execute("SELECT COUNT(*) FROM schedule")
	count = check[0][0] if check else -1

	if count == 0:
		await ctx.send("🔄 **Az adatbázis sikeresen törölve és alaphelyzetbe állítva!**")
	else:
		await ctx.send(f"⚠ **Hiba!** Az adatbázis törlése sikertelen! ({count} elem maradt)")

@tasks.loop(time=time(0, 0))
async def schedule_reset_task():
	try:
		await db_execute("UPDATE schedule SET participants=''", commit=True)
		print("🔄 Éjféli reset: Résztvevők törölve, újraregisztráció engedélyezve")

		records = await db_execute("SELECT id, server_name, time_slot, channel_id FROM schedule")
		for row in records:
			row_id, server_name, time_slot, channel_id = row

			channel = bot.get_channel(int(channel_id))
			if channel:
				await update_schedule_message(
					row_id,
					channel,
					server_name,
					time_slot,
					[],
					reset_flag=True
				)

	except Exception as e:
		print(f"⚠️ Éjféli reset hiba: {str(e)}")

@bot.event
async def setup_hook():
	await setup_database()

if __name__ == "__main__":
	bot.run(os.getenv('TOKEN'))