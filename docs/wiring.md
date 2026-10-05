# Wiring

The USB-UART adapter takes the place of the display: unplug the display and
connect the adapter to the display connector of the motor harness. Only
**three** wires go to the adapter — ground and the two data lines.

> [!CAUTION]
> The display connector also carries the **full battery voltage (36–52 V)**.
> Connected to the USB adapter it will destroy the adapter and can damage the
> computer. Identify every pin with a multimeter before connecting anything,
> insulate everything you do not use, and never plug or unplug connectors with
> the battery switched on.

## UART or CAN?

`bafang-cal` speaks the classic Bafang **UART** display protocol — the one the
C961 calibration display uses. Bafang moved the M620 to a CAN bus around 2021;
CAN systems need different tools (BESST or community CAN tools). Ways to tell:

| Clue | UART | CAN |
|------|------|-----|
| Display connector | green, **round** 5-pin (Higo/Julet) | green 5-pin with a "house"-shaped (pointed) face — the two do not mate |
| Motor label | `MM G510.1000` (no `.C`) | `MM G510.1000.C` |
| Controller label | `CR R10M.1000.SN.U 1.5` | firmware/labels with `R10MC` |
| Display model | `DP C18.UART`, `DP C961…` | `DP C18.CAN`, … |
| Production years | ≈ 2018–2020 | ≈ 2021 onwards |

If you used a C961 calibration display on this bike before, it has the UART
link. `bafang-cal probe` settles it: a UART controller answers it.

Bafang documents the calibration only for the controller
**CR R10M.1000.SN.U 1.5**; users report other controller versions refusing it.

## The display connector

| Function | Colour (Bafang display manuals) | Colour (many extension cables) | Goes to |
|----------|----------------------------------|--------------------------------|---------|
| Battery + (36–52 V) | red | brown | **nothing on the adapter** — see power lock |
| Power lock (switch-on input) | blue | orange | bridged to battery + to switch on |
| Ground | black | black | adapter **GND** |
| Data, controller → display ("RxD" of the display) | green | green or white | adapter **RX** |
| Data, display → controller ("TxD" of the display) | yellow | white or green | adapter **TX** |

Colours differ between harnesses and cable makers, so measure. Swapping the
two data wires is harmless: if `bafang-cal probe` gets no answer, swap them and
try again.

### Identifying the pins with a multimeter

With the battery switched **on** and the display unplugged, measure each pin
against ground (black):

1. **Battery +** reads the pack voltage (about 40–58 V on a 48 V pack).
2. **Power lock** reads about 0 V.
3. While power lock is bridged to battery + (below) the controller is on and
   its **data** output idles at its logic level — note whether that is about
   5 V or 3.3 V (see [adapter](#the-adapter)).

### Power lock

The controller only switches on while its power-lock input sees battery
voltage; inside a display a transistor connects the two when you press the
power button (the controller draws only about 50 mA through it). Without a
display you do the same:

- bridge power lock to battery + with a small switch or a removable jumper,
  well insulated — most DIY and commercial programming cables do exactly this;
- open the bridge (or switch the battery off) to power the controller down —
  this is also how you power-cycle it after the calibration.

Never route battery + or the power-lock wire to the USB adapter.

## The adapter

- **Chips:** CP2102, CH340, FT232R and PL2303 adapters have all been used with
  Bafang UART controllers. The USB programming cables sold for BBS02/BBSHD
  motors work on UART M620s too and already have the right plug.
- **Logic level:** Bafang BBS controllers use 5 V logic; the level on M620 UART
  controllers is not documented, and most commercial programming cables ship
  with their adapter set to 3.3 V and work. Measure the controller's idle data
  level (above) and set the adapter to match; if in doubt use the 5 V setting
  only with an adapter whose datasheet says its inputs tolerate it.
- Do **not** connect the adapter's VCC/5V/3V3 pin.
- Do **not** use an RS-232 (DB9) adapter: its ±12 V levels are not TTL.
- A **USB isolator** between computer and adapter is cheap insurance against
  wiring mistakes and ground loops. Otherwise prefer a laptop on battery.

```text
 USB-UART adapter            motor display connector
 ----------------            -----------------------
      GND  ----------------  ground (black)
      RX   ----------------  data, controller -> display (green)
      TX   ----------------  data, display -> controller (yellow)
      VCC  x  not connected
                             battery +  (red)  --+
                             power lock (blue) --+-- switch / jumper
```

## Building a cable

- **Programming cable:** a Bafang (or compatible) USB programming cable for BBS
  motors plugs straight into the UART display connector and bridges power lock
  for you. Check it with a multimeter anyway.
- **Extension cable:** a display extension cable for your system, cut in half,
  gives you the correct plug without modifying the bike's harness.
- **Temporary probing:** thin probe pins in the back of the connector work for
  a one-off calibration if they are insulated and cannot touch each other.

## Before you run the calibration

- [ ] adapter GND, RX and TX connected; nothing else on the adapter
- [ ] battery + and power lock bridged and insulated
- [ ] `bafang-cal probe` shows a status code and the battery level
- [ ] bike on a stand, chain off the chainring (or rear wheel lifted), drivetrain clear
- [ ] battery at least half charged
