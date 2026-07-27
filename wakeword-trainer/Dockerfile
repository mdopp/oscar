# The GPU worker that actually trains the "Solaris" microWakeWord model (#1066).
# TensorFlow-GPU because microWakeWord IS TensorFlow — this is not a torch job;
# the slim chat image could never host it, which is why the engine hands runs
# over through the wakeword_training_runs queue instead of launching anything.
FROM docker.io/tensorflow/tensorflow:2.18.0-gpu

# microWakeWord declares tensorflow>=2.18, so the base tag and this pin move
# together. microWakeWord publishes no release tags — pin the commit so a model
# shipped to the Voice PE is traceable to the framework that built it.
ARG MICROWAKEWORD_REF=4665173cd35f1cff9a61e06fc427f124766c488e
ARG PIPER_SAMPLE_GENERATOR_REF=v3.2.0

# git for the sources; wget+unzip for the negative spectrogram corpora the
# training script's phase 3 pulls from HuggingFace.
RUN apt-get update && apt-get install -y --no-install-recommends git wget unzip ca-certificates && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/kahrendt/microWakeWord /opt/microWakeWord \
 && git -C /opt/microWakeWord checkout ${MICROWAKEWORD_REF} \
 && git clone --depth 1 --branch ${PIPER_SAMPLE_GENERATOR_REF} https://github.com/rhasspy/piper-sample-generator /opt/piper-sample-generator

# microWakeWord's own deps only — piper-sample-generator pulls torch + piper-tts
# (~3 GB of CUDA wheels) for the positive-generation phase alone, so trainer.py
# installs those onto the /work volume at first claim rather than into an image
# that is already TensorFlow-GPU-sized.
RUN pip install --no-cache-dir -e /opt/microWakeWord

COPY train-micro-wake-word.py trainer.py /opt/

# TF_FORCE_GPU_ALLOW_GROWTH: ollama, whisper and Kokoro share this GPU —
# without it TF grabs all 16 GB VRAM the moment a run starts and takes the
# voice stack down with it.
ENV PYTHONUNBUFFERED=1 \
    SOLARIS_COMPONENT=wakeword-trainer \
    SOLARIS_DB_PATH=/var/lib/solaris/solaris.db \
    WAKEWORD_WORK_DIR=/work \
    TF_FORCE_GPU_ALLOW_GROWTH=true

ENTRYPOINT ["python", "/opt/trainer.py"]
