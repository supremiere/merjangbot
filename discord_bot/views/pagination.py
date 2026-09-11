# 시세·악보에서 함께 사용하는 이전·다음 버튼과 만료 처리를 제공합니다.
import discord


class PaginationView(discord.ui.View):
    def __init__(self, pages, timeout=300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.page = 0
        self.message = None
        self.prev_button = discord.ui.Button(
            label="◀ 이전", style=discord.ButtonStyle.secondary
        )
        self.page_button = discord.ui.Button(
            label="1", style=discord.ButtonStyle.secondary, disabled=True
        )
        self.next_button = discord.ui.Button(
            label="다음 ▶", style=discord.ButtonStyle.primary
        )
        self.prev_button.callback = self.go_previous
        self.next_button.callback = self.go_next
        for button in (self.prev_button, self.page_button, self.next_button):
            self.add_item(button)
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.pages) - 1
        self.page_button.label = f"{self.page + 1} / {len(self.pages)}"

    async def go_previous(self, interaction):
        self.page = max(0, self.page - 1)
        await self.show(interaction)

    async def go_next(self, interaction):
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self.show(interaction)

    async def show(self, interaction):
        self.update_buttons()
        await interaction.response.edit_message(embeds=self.pages[self.page], view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
