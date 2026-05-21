import base64
import logging
import os
import sys
import threading

from endcord import peripherals, terminal_utils

EXT_NAME = "Image Emoji"
EXT_VERSION = "0.2.0"
EXT_ENDCORD_VERSION = "1.5.0"
EXT_DESCRIPTION = "An extension that adds drawing custom discord emoji using kitty protocol"
EXT_SOURCE = "https://github.com/sparklost/endcord-image-emoji"
logger = logging.getLogger(__name__)

START_IMAGE_ID = 4000


def check_kitty():
    """Check if kitty protocol is supported"""
    response = terminal_utils.query_terminal(b"\x1b_Gi=1,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c")
    return "OK" in response


def kitty_upload_png(path, image_id):
    """Upload base64 encoded png into kitty image cache"""
    with open(path, "rb") as f:
        png_data = f.read()
    payload = base64.b64encode(png_data).decode("ascii")
    for i in range(0, len(payload), 4096):
        chunk = payload[i:i + 4096]
        more = 1 if i + 4096 < len(payload) else 0
        if i == 0:
            header = f"a=t,f=100,q=2,i={image_id},m={more}"
        else:
            header = f"m={more}"
        os.write(sys.stdout.fileno(), f"\033_G{header};{chunk}\033\\".encode())


def kitty_draw_image_by_id(image_id, x, y, w=None, h=None):
    """Draw previously uploaded image by its id"""
    os.write(sys.stdout.fileno(), f"\0337\033[{y+1};{x+1}H".encode())   # save and move cursor
    header = f"a=p,q=2,z=-1,i={image_id}"
    if w is not None:
        header += f",c={w}"
    if h is not None:
        header += f",r={h}"
    os.write(sys.stdout.fileno(), f"\033_G{header}\033\\\0338".encode())   # \0338 is restore cursor


def kitty_delete_images_by_id(image_id):
    """Delete all images with this id and remove it from memory"""
    os.write(sys.stdout.fileno(), f"\033_Ga=d,d=I,q=2,i={image_id}\033\\".encode())


def kitty_clear_images_by_id(image_id):
    """Delete all images with this id but keep image in memory"""
    os.write(sys.stdout.fileno(), f"\033_Ga=d,d=i,q=2,i={image_id}\033\\".encode())


class Extension:
    """Main extension class"""

    def __init__(self, app):
        self.app = app
        kitty_supported = getattr(self.app.tui, "kitty_supported", None)
        if kitty_supported is False or (kitty_supported is not True and not check_kitty()):
            logger.warning("No kitty protocol support detected in this terminal")
            self.run = False
            del type(self).on_chat_update
            del type(self).on_chat_draw
            self.app.tui.kitty_supported = False
            return

        self.app.placeholder_emoji = True
        self.app.formatter.placeholder_emoji = "  "

        self.run = True
        self.chat_map = []
        self.update = threading.Event()
        self.drawing = threading.Event()
        self.image_ids = {}
        self.emoji_pos_cache = []
        self.prev_chat_index = None
        self.prev_chat_hw = None
        self.prew_win_hw = self.app.tui.screen_hw
        self.force_draw = False
        self.image_ids_lock = threading.Lock()
        self.post_one_reaction_len = len(self.app.config["format_one_reaction"].split("%reaction")[-1])
        threading.Thread(target=self.worker, daemon=True).start()


    def on_chat_update(self, chat, chat_format, chat_map):   # noqa
        """Get new chat map"""
        self.chat_map = chat_map
        self.update.set()


    def on_chat_draw(self):
        """Re-calculate image positions and draw them"""
        if not self.force_draw and self.prev_chat_index == self.app.tui.chat_index and self.prev_chat_hw == self.app.tui.chat_hw:
            return
        if self.prew_win_hw != self.app.tui.screen_hw:
            self.prew_win_hw = self.app.tui.screen_hw
            self.reupload_all()
        _, chat_x = self.app.tui.win_chat.getbegyx()
        chat_h = self.app.tui.chat_hw[0]
        with self.app.tui.lock:
            with self.image_ids_lock:
                for kitty_image_id in self.image_ids.values():
                    kitty_clear_images_by_id(kitty_image_id)
            for rel_y, rel_x, kitty_image_id in self.emoji_pos_cache:
                abs_y = chat_h - (rel_y - self.app.tui.chat_index - self.app.tui.have_title + 1)
                if abs_y <= 0 or abs_y > chat_h:
                    continue
                abs_x = chat_x + rel_x
                kitty_draw_image_by_id(kitty_image_id, x=abs_x, y=abs_y, w=None, h=1)
        self.prev_chat_index = self.app.tui.chat_index
        self.prev_chat_hw = self.app.tui.chat_hw


    def reupload_all(self):
        """Delete all images and trigger reupload"""
        for image in self.image_ids.values():
            with self.app.tui.lock:
                kitty_delete_images_by_id(image)
        self.image_ids = {}
        self.emoji_pos_cache = []
        self.update.set()


    def get_free_id(self):
        """Get first free id"""
        ids = self.image_ids.values()
        for i in range(len(ids) - 1):
            if ids[i + 1] != ids[i] + 1:
                return ids[i] + 1
        if START_IMAGE_ID not in ids:
            return START_IMAGE_ID
        return START_IMAGE_ID + len(ids)


    def worker(self):
        """Thread that updates emoji cache on disk and in ram and downloads missing emoji"""
        while self.run:
            self.update.wait()
            self.update.clear()
            visible = []
            new_emoji_pos_cache = []
            new_image_ids = []
            self.force_draw = False

            for rel_y, line_map in enumerate(self.chat_map):
                if not line_map:
                    continue
                if line_map[3]:
                    iterable = line_map[3]
                elif line_map[5] and line_map[5][2]:
                    iterable = line_map[5][2]
                else:
                    iterable = ()

                for emoji in iterable:
                    if line_map[3]:
                        _, rel_x, emoji_id = emoji
                        rel_x -= 1 + self.post_one_reaction_len
                    else:
                        rel_x, _, emoji_id = emoji
                    if not emoji_id:
                        continue

                    if emoji_id in self.image_ids:
                        kitty_image_id = self.image_ids[emoji_id]
                    else:
                        image_path = self.app.discord.get_emoji(emoji_id, size=None, img_type="png", cache=os.path.join(peripherals.cache_path, "emoji"))
                        kitty_image_id = self.get_free_id()
                        with self.app.tui.lock:
                            kitty_upload_png(image_path, kitty_image_id)
                        new_image_ids.append((emoji_id, kitty_image_id))
                        self.force_draw = True

                    visible.append(emoji_id)
                    new_emoji_pos_cache.append((rel_y, rel_x, kitty_image_id))

            # update cahanged images
            if new_emoji_pos_cache != self.emoji_pos_cache or self.force_draw:
                self.emoji_pos_cache = new_emoji_pos_cache
                self.force_draw = True
                self.on_chat_draw()

            # delete unused cache
            deleted_kitty = []
            with self.image_ids_lock:
                for emoji_id, kitty_image_id in new_image_ids:
                    self.image_ids[emoji_id] = kitty_image_id
                to_delete = [k for k in self.image_ids if k not in visible]
                for emoji_id in to_delete:
                    deleted_kitty.append(self.image_ids.pop(emoji_id))
            with self.app.tui.lock:
                for kitty_image_id in deleted_kitty:
                    if kitty_image_id not in self.image_ids.values():
                        kitty_delete_images_by_id(kitty_image_id)
