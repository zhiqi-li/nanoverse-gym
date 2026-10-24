"""Model identities accepted at the Gym/native actor boundary."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    transport: str


PROFILES = {
    'gpt-6.1-sol': ModelProfile('gpt-6.1-sol', 'openai/openai/gpt-6.1-sol', 'responses'),
    'nemotron-3.5-super-vl': ModelProfile('nemotron-3.5-super-vl', 'nvidia/nvidia/nemotron-3.5-super-vl', 'chat'),
    'nemotron-3.5-super-vl-nim': ModelProfile('nemotron-3.5-super-vl-nim', 'nvidia/nemotron-3.5-super-vl-120b-a12b', 'chat'),
}


def model_profile(model):
    for profile in PROFILES.values():
        if profile.model == model:
            return profile
    raise ValueError(f'Unsupported model identity: {model}')
