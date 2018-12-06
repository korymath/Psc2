"""Setup for CANARIE talk.
This is a server that communicates with SuperCollider for interacting with a
MIDI controller.

It handles all the logic for figuring out when to turn on/off the bass notes.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl import app

import importlib
import os
import OSC
import threading
import tensorflow as tf

import magenta
from magenta.models.melody_rnn import melody_rnn_model
from magenta.models.melody_rnn import melody_rnn_generate
from magenta.models.melody_rnn import melody_rnn_sequence_generator
from magenta.protobuf import generator_pb2

importlib.import_module('ascii_arts')
import ascii_arts

# Global Variables
# Addresses and ports to communicate with SuperCollider.
receive_address = ('127.0.0.1', 12345)
send_address = ('127.0.0.1', 57120)
server = OSC.OSCServer(receive_address)  # To receive from SuperCollider.
client = OSC.OSCClient()  # To send to SuperCollier.
client.connect(send_address)
mode = 'psc'  # Current mode of operation: {'psc', 'robot', 'improv'}

# Robot improv specific variables.
min_primer_length = 20
max_robot_length = 20
accumulated_primer_melody = []
generated_melody = []
# Mapping of notes (defaults to identity).
note_mapping = {i:i for i in range(21, 109)}
improv_mode = '2sounds'  # Either '2sounds', '1sound', 'question'.
improv_status = 'psc'  # One of 'psc' or 'robot'.

# Read in the PerformanceRNN model.
MODEL_PATH = '~/Psc2/magenta_models/attention_rnn.mag'
bundle = magenta.music.read_bundle_file(MODEL_PATH)


def generate_melody():
  """Generate a new melody by querying the model."""
  global bundle
  global accumulated_primer_melody
  global generated_melody
  global max_robot_length
  config_id = bundle.generator_details.id
  config = melody_rnn_model.default_configs[config_id]
  generator = melody_rnn_sequence_generator.MelodyRnnSequenceGenerator(
      model=melody_rnn_model.MelodyRnnModel(config),
      details=config.details,
      steps_per_quarter=config.steps_per_quarter,
      checkpoint=melody_rnn_generate.get_checkpoint(),
      bundle=bundle)
  generator_options = generator_pb2.GeneratorOptions()
  generator_options.args['temperature'].float_value = 1.0
  generator_options.args['beam_size'].int_value = 1
  generator_options.args['branch_factor'].int_value = 1
  generator_options.args['steps_per_iteration'].int_value = 1
  primer_melody = magenta.music.Melody(accumulated_primer_melody)
  qpm = magenta.music.DEFAULT_QUARTERS_PER_MINUTE
  primer_sequence = primer_melody.to_sequence(qpm=qpm)
  seconds_per_step = 60.0 / qpm / generator.steps_per_quarter
  # Set the start time to begin on the next step after the last note ends.
  last_end_time = (max(n.end_time for n in primer_sequence.notes)
                   if primer_sequence.notes else 0)
  total_seconds = last_end_time * 3
  generate_section = generator_options.generate_sections.add(
      start_time=last_end_time + seconds_per_step,
      end_time=total_seconds)
  generated_sequence = generator.generate(primer_sequence, generator_options)
  generated_melody = [n.pitch for n in generated_sequence.notes]
  # Get rid of primer melody.
  generated_melody = generated_melody[len(accumulated_primer_melody):]
  # Make sure generated melody is not too long.
  generated_melody = generated_melody[:max_robot_length]
  accumulated_primer_melody = []



def send_playnote(note=None, sound='wurly'):
  """Send a `playnote` message to SuperCollider.

  Will send different commands based on the sound desired.
  """
  global mode
  if note is None:
    return
  note[1] = max(1, note[1])
  msg = OSC.OSCMessage()
  command = '/play{}'.format(sound)
  msg.setAddress(command)
  msg.append(note)
  client.send(msg)


def send_stopnote(note):
  """Send a `stopnote` message to SuperCollider.
  """
  if note is None:
    return
  msg = OSC.OSCMessage()
  msg.setAddress('/stopnote')
  msg.append(note)
  client.send(msg)


def process_note_on(addr, tags, args, source):
  """Handler for `/processnoteon` messages from SuperCollider.

  This will process the event of a key press on the MIDI controller, detected
  by SuperCollider. Depending on the state of the server it will do different
  things:
  - mode == 'improv': Decide, based on whether a generated melody is ready,
      to hijack my notes with those generated by the model. The value of
      'improv_mode' determines what sound is sent:
    - if improv_mode == '2sounds' use 'organ' for robot, 'wurly' for human.
    - if improv_mode == 'question' use only 'wurly'
  - otherwise determine whether I'm playing the lower or upper half of the
    iRig keyboard:
    - If lower half, send same notes with 'wurly' sound.
    - If upper half, transpose notes an octave down and send 'organ' sound.

  Args:
    addr: Address message sent to.
    tags: Tags in message.
    args: Arguments passed in message.
    source: Source of sender.
  """
  global accumulated_primer_melody
  global generated_melody
  global min_primer_length
  global note_mapping
  global improv_mode
  global improv_status
  global mode
  global start_time
  print_status()
  note = list(args)
  if mode == 'improv':
    # If we have data in our generated melody we substitute human's notes.
    if len(generated_melody):
      improv_status = 'question' if improv_mode == 'question' else 'robot'
      # To avoid stuck notes, send a note off for previous mapped note.
      prev_note = list(args)
      prev_note[0] = note_mapping[args[0]]
      send_stopnote(prev_note)
      note_mapping[args[0]] = generated_melody[0]
      note[0] = generated_melody[0]
      generated_melody = generated_melody[1:]
      sound = 'organ' if improv_mode == '2sounds' else 'wurly'
    else:
      improv_status = 'question' if improv_mode == 'question' else 'psc'
      accumulated_primer_melody.append(args[0])
      sound = 'wurly'
    if len(accumulated_primer_melody) >= min_primer_length:
      magenta_thread = threading.Thread(target = generate_melody)
      magenta_thread.start()
  else:
    if note[0] >= 72:
      sound = 'organ'
      improv_status = 'robot'
      note[0] = note[0] - 12
      print_status()
    else:
      sound = 'wurly'
      improv_status = 'psc'
      print_status()
  send_playnote(note, sound)


def process_note_off(addr, tags, args, source):
  """Handler for `/processnoteoff` messages from SuperCollider.

  If we're in improv_status == 'robot' and not improv mode, transpose
  notes an octave down, since that's what we did when sending the
  note to play.

  Args:
    addr: Address message sent to.
    tags: Tags in message.
    args: Arguments passed in message.
    source: Source of sender.
  """
  global mode
  global note_mapping
  orig_note = list(args)
  note = list(args)
  note[0] = note_mapping[args[0]]
  if improv_status == 'robot' and mode != 'improv' and note[0] >= 72:
    note[0] -= 12
  orig_note[0] = args[0]
  send_stopnote(note)
  # Just in case we also send a stopnote for original note.
  send_stopnote(orig_note)
  note_mapping[args[0]] = args[0]


def cc_event(addr, tags, args, source):
  """Handler for `/ccevent` messages from SuperCollider.

  Logic for dealine with the volume knob in the iRig mini
  keyboard.
  - If in lowest position (0): mode = 'psc' (wurly sound for
      lower half, organ sound for upper half, no pitch hijacking).
  - Else if in lower half of range, mode = '2sounds' (pitch hijacking,
      wurly sound for human, organ for robot).
  - Else if in upper half (but not max), mode = '1sound' (pitch hijacking,
      wurly sound for both human and organ).
  - Else if at highest position (127): mode = 'question' (pitch hijacking,
      wurly sound for both, no faces displayed, only a question mark).

  Args:
    addr: Address message sent to.
    tags: Tags in message.
    args: Arguments passed in message.
    source: Source of sender.
  """
  global mode
  global improv_mode
  if not args:
    return
  cc_num, cc_chan, cc_src, cc_args = args
  if cc_num == 0:
    mode = 'psc'
    print_status()
    return
  mode = 'improv'
  if cc_num < 64:
    improv_mode = '2sounds'
  elif cc_num < 127:
    improv_mode = '1sound'
  elif cc_num == 127:
    improv_mode = 'question'
  print_status()


def print_status():
  """Prints the "face" of the user playing, unless in 'question' mode."""
  global mode
  global improv_mode
  global improv_status
  global min_primer_length
  global max_robot_length
  global accumulated_primer_melody
  global generated_melody
  os.system('clear')
  print(ascii_arts.arts[improv_status])
  if mode != 'improv':
    return
  if improv_status == 'psc':
    print('*' * len(accumulated_primer_melody) +
          '-' * (min_primer_length - len(accumulated_primer_melody)))
  elif improv_status == 'robot':
    print('-' * (max_robot_length - len(generated_melody)) +
          '*' * (len(generated_melody)))


def main(_):
  tf.logging.set_verbosity(tf.logging.ERROR)
  print_status()
  
  # Set up and start the server.
  st = threading.Thread(target = server.serve_forever)
  st.start()
  server.addMsgHandler('/processnoteon', process_note_on)
  server.addMsgHandler('/processnoteoff', process_note_off)
  server.addMsgHandler('/ccevent', cc_event)


if __name__ == '__main__':
  app.run(main)
