"""refund_standard@v0: hand-written against docs/skill_abi.md (Phase 0, section 2.5).

The executable spec of the ABI: codegen's drafts should look like this, and
the executor must run it unchanged. Happy path of section 11.
"""


def run(ctx, args):
    ctx.step(1)
    order = ctx.call("get_order", order_id=args["order_id"])
    ctx.bind("order", order)

    ctx.step(2)
    shipments = ctx.call("get_shipments", order_id=order["order_id"])
    ctx.bind("shipments", shipments)

    ctx.step(3)
    amount = ctx.call("compute_amount", total=order["total"], discount=order["discount"])["amount"]
    ctx.bind("amount", amount)

    ctx.step(4)
    refund = ctx.call("payments.refund", charge_id=order["charge_id"], amount=amount)
    ctx.bind("refund", refund)

    ctx.step(5)
    ticket = ctx.call("tickets.update", ticket_id=order["ticket_id"], status="resolved")
    ctx.bind("ticket", ticket)

    ctx.step(6)
    email_body = ctx.hole("email_body", inputs={"order_id": args["order_id"], "amount": amount})
    ctx.call("email.send", to=order["customer_email"], subject="Your refund has been processed", body=email_body)

    return {"ok": True}
