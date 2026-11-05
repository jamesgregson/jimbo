from .mailer import send
from .pricing import format_price


def render_email(order, settings):
    lines = [f"Thanks for your order #{order['id']}.", ""]
    for item in order["items"]:
        lines.append(f"{item['qty']} x {item['sku']}")
    lines.append(f"Total: {format_price(order['total'])}")
    return "\n".join(lines)


def send_confirmation(order, settings):
    to = order["customer"].get("email").strip().lower()
    send(settings["sender"], to, "Your order", render_email(order, settings))
