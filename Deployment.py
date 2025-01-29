import os
import discord
from discord.ext import commands, tasks
import sqlite3
import asyncio
from datetime import datetime, timedelta, time
from typing import List, Tuple, Union, Optional
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def get_db_connection():
	return sqlite3.connect("schedule.db")

def db_execute(query: str, params: tuple = (), commit: bool = False) -> list:
	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute(query, params)
	result = cursor.fetchall()
	if commit:
		conn.commit()
	conn.close()
	return result

def setup_database():
	db_execute("""
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
		today = datetime.now().strftime("%Y-%m-%d")

		start = datetime.strptime(f"{today} {start_str.strip()}:00", "%Y-%m-%d %H:%M:%S")
		end = datetime.strptime(f"{today} {end_str.strip()}:00", "%Y-%m-%d %H:%M:%S")

		if end < start:
			end += timedelta(days=1)

		return start, end
	except ValueError:
		raise commands.BadArgument("❌ Hibás időformátum! Használd a **HH:MM-HH:MM** formátumot.")

def create_event_embed(server_name: str, time_slot: str, participants: List[str], end_time: datetime) -> discord.Embed:
	now = datetime.now()
	embed = discord.Embed(
		title=f"🎮 {server_name}",
		description="――――――――――――――――――――",
		color=discord.Color.red() if now >= end_time else discord.Color.green()
	)
	embed.add_field(
		name="🕒 Időpont",
		value=f"**`{time_slot}`**",
		inline=False
	)
	embed.add_field(
		name="👥 Résztvevők",
		value="• ".join([p.split(":")[1] for p in participants]) if participants else "Senki",
		inline=True
	)
	return embed

async def schedule_event_end(row_id: int, channel_id: int, server_name: str, time_slot: str):
	_, end_time = parse_time_slot(time_slot)
	now = datetime.now()

	delay = (end_time - now).total_seconds()
	if delay > 0:
		await asyncio.sleep(delay)

	db_execute("UPDATE schedule SET participants='' WHERE id=?", (row_id,), commit=True)
	channel = bot.get_channel(channel_id)

	if channel:
		await update_schedule_message(row_id, channel, server_name, time_slot, [])

	print(f"⏳ Esemény lezárva: {server_name} ({time_slot})")

def get_participants_list(participants: Union[str, List[str]]) -> List[str]:
	if isinstance(participants, str):
		return participants.split(", ") if participants else []
	return participants

async def update_schedule_message(row_id: int, channel: discord.TextChannel, server_name: str, time_slot: str, participants: List[str], reset_flag: bool = False):
	result = db_execute("SELECT message_id FROM schedule WHERE id=?", (row_id,))
	message_id = result[0][0] if result and result[0][0] else None

	start_time, end_time = parse_time_slot(time_slot)
	now = datetime.now()

	embed = create_event_embed(server_name, time_slot, participants, end_time)

	view = discord.ui.View()

	if now >= end_time and not reset_flag:
		view.add_item(EventEndedButton())
	else:
		if reset_flag:
			participants = []

		view.add_item(JoinButton(row_id, server_name, participants, time_slot))
		view.add_item(LeaveButton(row_id, server_name, participants, time_slot))

	if message_id:
		try:
			message = await channel.fetch_message(int(message_id))
			await message.edit(embed=embed, view=view)
		except (discord.NotFound, ValueError):
			new_message = await channel.send(embed=embed, view=view)
			db_execute("UPDATE schedule SET message_id=? WHERE id=?", (new_message.id, row_id), commit=True)
	else:
		new_message = await channel.send(embed=embed, view=view)
		db_execute("UPDATE schedule SET message_id=? WHERE id=?", (new_message.id, row_id), commit=True)

class JoinButton(discord.ui.Button):
	def __init__(self, row_id: int, server_name: str, participants: List[str], time_slot: str):
		super().__init__(label="✅ Csatlakozás", style=discord.ButtonStyle.success)
		self.row_id = row_id
		self.server_name = server_name
		self.participants = participants
		self.time_slot = time_slot

	async def callback(self, interaction: discord.Interaction):
		user_id = str(interaction.user.id)
		user_name = interaction.user.display_name
		current_participants = get_participants_list(
			db_execute("SELECT participants FROM schedule WHERE id=?", (self.row_id,))[0][0]
		)

		if not any(user_id in p for p in current_participants):
			current_participants.append(f"{user_id}:{user_name}")

		db_execute(
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

class LeaveButton(discord.ui.Button):
	def __init__(self, row_id: int, server_name: str, participants: List[str], time_slot: str):
		super().__init__(label="❌ Kilépés", style=discord.ButtonStyle.danger)
		self.row_id = row_id
		self.server_name = server_name
		self.participants = participants
		self.time_slot = time_slot

	async def callback(self, interaction: discord.Interaction):
		user_id = str(interaction.user.id)
		current_participants = get_participants_list(
			db_execute("SELECT participants FROM schedule WHERE id=?", (self.row_id,))[0][0]
		)

		current_participants = [p for p in current_participants if user_id not in p]

		db_execute(
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
	def __init__(self):
		super().__init__(label="⏳ Esemény lejárt", style=discord.ButtonStyle.secondary, disabled=True)


@bot.event
async def on_ready():
	print(f"{bot.user} elindult!")
	await bot.wait_until_ready()

	if not schedule_reset_task.is_running():
		schedule_reset_task.start()
		print("⏳ Reset loop elindítva!")
	else:
		print("⚠️ A reset loop már fut!")

message_sending_enabled = False  # Alapértelmezés szerint NÉMA

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
	db_execute("DELETE FROM schedule", commit=True)
	db_execute("DELETE FROM sqlite_sequence WHERE name='schedule'", commit=True)
	db_execute("VACUUM", commit=True)

	check = db_execute("SELECT COUNT(*) FROM schedule")
	count = check[0][0] if check else -1

	if count == 0:
		await ctx.send("🔄 **Az adatbázis sikeresen törölve és alaphelyzetbe állítva!**")
	else:
		await ctx.send(f"⚠ **Hiba!** Az adatbázis törlése sikertelen! ({count} elem maradt)")

@bot.command()
async def add_server(ctx, server_name: str, time_slot: str):
	try:
		start_time, end_time = parse_time_slot(time_slot)
	except commands.BadArgument as e:
		if message_sending_enabled:
			await ctx.send(str(e))
		return

	existing = db_execute("SELECT id FROM schedule WHERE server_name=? AND time_slot=?", (server_name, time_slot))
	if existing:
		if message_sending_enabled:
			await ctx.send(f"⚠ A(z) {server_name} már létezik ebben az időpontban!")
		return

	db_execute(
		"INSERT INTO schedule (server_name, time_slot, participants, channel_id) VALUES (?, ?, '', ?)",
		(server_name, time_slot, ctx.channel.id),
		commit=True
	)

	row_id = db_execute("SELECT id FROM schedule WHERE server_name=? AND time_slot=?", (server_name, time_slot))[0][0]

	channel = ctx.channel
	await update_schedule_message(row_id, channel, server_name, time_slot, [])

	bot.loop.create_task(schedule_event_end(row_id, channel.id, server_name, time_slot))

	if message_sending_enabled:
		await show_schedule(ctx)

	await ctx.message.delete()

@bot.command()
async def remove_server(ctx, row_id: int):
	result = db_execute("SELECT message_id, channel_id FROM schedule WHERE id=?", (row_id,))
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

	db_execute("DELETE FROM schedule WHERE id=?", (row_id,), commit=True)
	db_execute("VACUUM", commit=True)

	check = db_execute("SELECT COUNT(*) FROM schedule WHERE id=?", (row_id,))
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
	records = db_execute("SELECT id, server_name, time_slot, participants FROM schedule")

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

@tasks.loop(time=time(0, 0))
#@tasks.loop(minutes=3)
async def schedule_reset_task():
	db_execute("UPDATE schedule SET participants=''", commit=True)
	print("🔄 Éjféli reset: Résztvevők törölve, újraregisztráció engedélyezve")

	records = db_execute("SELECT id, server_name, time_slot, channel_id FROM schedule")
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

if __name__ == "__main__":
	setup_database()
	bot.run(os.getenv('TOKEN'))