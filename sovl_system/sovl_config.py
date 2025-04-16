import json
import os
import gzip
import hashlib
from typing import Any, Optional, Dict, List, Union, Callable
from dataclasses import dataclass
from threading import Lock
import traceback
import re
import time
from sovl_logger import Logger
from transformers import AutoConfig

@dataclass
class ConfigSchema:
    """Defines validation rules for configuration fields."""
    field: str
    type: type
    default: Any = None
    validator: Optional[Callable[[Any], bool]] = None
    range: Optional[tuple] = None
    required: bool = False
    nullable: bool = False

class _SchemaValidator:
    """Handles configuration schema validation logic."""
    
    def __init__(self, logger: Logger):
        self.logger = logger
        self.schemas: Dict[str, ConfigSchema] = {}

    def register(self, schemas: List[ConfigSchema]):
        """Register new schemas."""
        self.schemas.update({s.field: s for s in schemas})

    def validate(self, key: str, value: Any, conversation_id: str = "init") -> tuple[bool, Any]:
        """Validate a value against its schema."""
        schema = self.schemas.get(key)
        if not schema:
            self.logger.record({
                "error": f"Unknown configuration key: {key}",
                "timestamp": time.time(),
                "conversation_id": conversation_id
            })
            return False, None

        if value is None:
            if schema.required:
                self.logger.record({
                    "error": f"Required field {key} is missing",
                    "suggested": f"Set to default: {schema.default}",
                    "timestamp": time.time(),
                    "conversation_id": conversation_id
                })
                return False, schema.default
            if schema.nullable:
                return True, value
            return False, schema.default

        if not isinstance(value, schema.type):
            self.logger.record({
                "warning": f"Invalid type for {key}: expected {schema.type.__name__}, got {type(value).__name__}",
                "suggested": f"Set to default: {schema.default}",
                "timestamp": time.time(),
                "conversation_id": conversation_id
            })
            return False, schema.default

        if schema.validator and not schema.validator(value):
            valid_options = getattr(schema.validator, '__doc__', '') or str(schema.validator)
            self.logger.record({
                "warning": f"Invalid value for {key}: {value}",
                "suggested": f"Valid options: {valid_options}, default: {schema.default}",
                "timestamp": time.time(),
                "conversation_id": conversation_id
            })
            return False, schema.default

        if schema.range and not (schema.range[0] <= value <= schema.range[1]):
            self.logger.record({
                "warning": f"Value for {key} out of range {schema.range}: {value}",
                "suggested": f"Set to default: {schema.default}",
                "timestamp": time.time(),
                "conversation_id": conversation_id
            })
            return False, schema.default

        return True, value

class _ConfigStore:
    """Manages configuration storage and structure."""

    def __init__(self):
        self.flat_config: Dict[str, Any] = {}
        self.structured_config: Dict[str, Any] = {
            "core_config": {},
            "lora_config": {},
            "training_config": {"dry_run_params": {}},
            "curiosity_config": {},
            "cross_attn_config": {},
            "controls_config": {},
            "logging_config": {},
        }
        self.cache: Dict[str, Any] = {}

    def set_value(self, key: str, value: Any):
        """Set a value in flat and structured configs."""
        self.cache[key] = value
        keys = key.split('.')
        if len(keys) == 2:
            section, field = keys
            self.flat_config.setdefault(section, {})[field] = value
            self.structured_config[section][field] = value
        elif len(keys) == 3 and keys[0] == "training_config" and keys[1] == "dry_run_params":
            section, sub_section, field = keys
            self.flat_config.setdefault(section, {}).setdefault(sub_section, {})[field] = value
            self.structured_config[section][sub_section][field] = value

    def get_value(self, key: str, default: Any) -> Any:
        """Retrieve a value from the configuration."""
        if key in self.cache:
            return self.cache[key]
        keys = key.split('.')
        value = self.flat_config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        return value if value != {} and value is not None else default

    def get_section(self, section: str) -> Dict[str, Any]:
        """Get an entire configuration section."""
        return self.structured_config.get(section, {})

    def rebuild_structured(self, schemas: List[ConfigSchema]):
        """Rebuild structured config from flat config."""
        for schema in schemas:
            keys = schema.field.split('.')
            section = keys[0]
            if len(keys) == 2:
                field = keys[1]
                self.structured_config[section][field] = self.get_value(schema.field, schema.default)
            elif len(keys) == 3 and section == "training_config" and keys[1] == "dry_run_params":
                field = keys[2]
                self.structured_config[section]["dry_run_params"][field] = self.get_value(schema.field, schema.default)

    def update_cache(self, schemas: List[ConfigSchema]):
        """Update cache with current config values."""
        self.cache = {schema.field: self.get_value(schema.field, schema.default) for schema in schemas}

class _FileHandler:
    """Handles configuration file operations."""

    def __init__(self, config_file: str, logger: Logger):
        self.config_file = config_file
        self.logger = logger

    def load(self, max_retries: int = 3) -> Dict[str, Any]:
        """Load configuration file with retry logic."""
        for attempt in range(max_retries):
            try:
                if not os.path.exists(self.config_file):
                    return {}
                if self.config_file.endswith('.gz'):
                    with gzip.open(self.config_file, 'rt', encoding='utf-8') as f:
                        return json.load(f)
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, gzip.BadGzipFile) as e:
                self.logger.record({
                    "error": f"Attempt {attempt + 1} failed to load config {self.config_file}: {str(e)}",
                    "timestamp": time.time(),
                    "stack_trace": traceback.format_exc(),
                    "conversation_id": "init"
                })
                if attempt == max_retries - 1:
                    return {}
            time.sleep(0.1)
        return {}

    def save(self, config: Dict[str, Any], file_path: Optional[str] = None, compress: bool = False, max_retries: int = 3) -> bool:
        """Save configuration to file atomically."""
        save_path = file_path or self.config_file
        temp_file = f"{save_path}.tmp"
        for attempt in range(max_retries):
            try:
                if compress:
                    with gzip.open(temp_file, 'wt', encoding='utf-8') as f:
                        json.dump(config, f, indent=2)
                else:
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        json.dump(config, f, indent=2)
                os.replace(temp_file, save_path)
                self.logger.record({
                    "event": "config_save",
                    "file_path": save_path,
                    "compressed": compress,
                    "timestamp": time.time(),
                    "conversation_id": "init"
                })
                return True
            except Exception as e:
                self.logger.record({
                    "error": f"Attempt {attempt + 1} failed to save config to {save_path}: {str(e)}",
                    "timestamp": time.time(),
                    "stack_trace": traceback.format_exc(),
                    "conversation_id": "init"
                })
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                if attempt == max_retries - 1:
                    return False
                time.sleep(0.1)
        return False

class ConfigManager:
    """Manages SOVLSystem configuration with validation, thread safety, and persistence."""

    DEFAULT_SCHEMA = [
        # core_config
        ConfigSchema("core_config.base_model_name", str, "gpt2", required=True),
        ConfigSchema("core_config.scaffold_model_name", str, "gpt2", required=True),
        ConfigSchema("core_config.cross_attn_layers", list, [5, 7], lambda x: all(isinstance(i, int) for i in x)),
        ConfigSchema("core_config.use_dynamic_layers", bool, False),
        ConfigSchema("core_config.layer_selection_mode", str, "balanced", lambda x: x in ["balanced", "random", "fixed"]),
        ConfigSchema("core_config.custom_layers", list, None, lambda x: x is None or all(isinstance(i, int) for i in x), nullable=True),
        ConfigSchema("core_config.valid_split_ratio", float, 0.2, range=(0.0, 1.0)),
        ConfigSchema("core_config.random_seed", int, 42, range=(0, 2**32)),
        ConfigSchema("core_config.quantization", str, "fp16", lambda x: x in ["fp16", "int8", "fp32"]),
        ConfigSchema("core_config.hidden_size", int, 768, range=(128, 4096)),
        # lora_config
        ConfigSchema("lora_config.lora_rank", int, 8, range=(1, 64)),
        ConfigSchema("lora_config.lora_alpha", int, 16, range=(1, 128)),
        ConfigSchema("lora_config.lora_dropout", float, 0.1, range=(0.0, 0.5)),
        ConfigSchema("lora_config.lora_target_modules", list, ["c_attn", "c_proj", "c_fc"], lambda x: all(isinstance(i, str) for i in x)),
        # training_config
        ConfigSchema("training_config.learning_rate", float, 0.0003, range=(0.0, 0.01)),
        ConfigSchema("training_config.train_epochs", int, 3, range=(1, 10)),
        ConfigSchema("training_config.batch_size", int, 1, range=(1, 64)),
        ConfigSchema("training_config.max_seq_length", int, 128, range=(64, 2048)),
        ConfigSchema("training_config.sigmoid_scale", float, 0.5, range=(0.1, 10.0)),
        ConfigSchema("training_config.sigmoid_shift", float, 5.0, range=(0.0, 10.0)),
        ConfigSchema("training_config.lifecycle_capacity_factor", float, 0.01, range=(0.0, 1.0)),
        ConfigSchema("training_config.lifecycle_curve", str, "sigmoid_linear", lambda x: x in ["sigmoid_linear", "linear", "exponential"]),
        ConfigSchema("training_config.accumulation_steps", int, 4, range=(1, 16)),
        ConfigSchema("training_config.exposure_gain_eager", int, 3, range=(1, 10)),
        ConfigSchema("training_config.exposure_gain_default", int, 2, range=(1, 10)),
        ConfigSchema("training_config.max_patience", int, 2, range=(1, 5)),
        ConfigSchema("training_config.sleep_max_steps", int, 100, range=(10, 1000)),
        ConfigSchema("training_config.lora_capacity", int, 0, range=(0, 1000)),
        ConfigSchema("training_config.dry_run", bool, False),
        ConfigSchema("training_config.dry_run_params.max_samples", int, 2, range=(1, 100)),
        ConfigSchema("training_config.dry_run_params.max_length", int, 128, range=(64, 2048)),
        ConfigSchema("training_config.dry_run_params.validate_architecture", bool, True),
        ConfigSchema("training_config.dry_run_params.skip_training", bool, True),
        # New training config fields
        ConfigSchema("training_config.weight_decay", float, 0.01, range=(0.0, 0.1)),
        ConfigSchema("training_config.total_steps", int, 1000, range=(100, 10000)),
        ConfigSchema("training_config.max_grad_norm", float, 1.0, range=(0.1, 10.0)),
        ConfigSchema("training_config.use_amp", bool, True),
        ConfigSchema("training_config.checkpoint_interval", int, 1000, range=(100, 10000)),
        ConfigSchema("training_config.scheduler_type", str, "linear", lambda x: x in ["linear", "cosine", "constant"]),
        ConfigSchema("training_config.cosine_min_lr", float, 1e-6, range=(1e-7, 1e-3)),
        ConfigSchema("training_config.warmup_ratio", float, 0.1, range=(0.0, 0.5)),
        ConfigSchema("training_config.metrics_to_track", list, ["loss", "accuracy", "confidence"], lambda x: all(isinstance(i, str) for i in x)),
        ConfigSchema("training_config.repetition_n", int, 3, range=(1, 10)),
        ConfigSchema("training_config.checkpoint_path", str, "checkpoints/sovl_trainer"),
        ConfigSchema("training_config.validate_every_n_steps", int, 100, range=(10, 1000)),
        # curiosity_config
        ConfigSchema("curiosity_config.queue_maxlen", int, 10, range=(1, 50)),
        ConfigSchema("curiosity_config.novelty_history_maxlen", int, 20, range=(5, 100)),
        ConfigSchema("curiosity_config.decay_rate", float, 0.9, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.attention_weight", float, 0.5, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.question_timeout", float, 3600.0, range=(60.0, 86400.0)),
        ConfigSchema("curiosity_config.novelty_threshold_spontaneous", float, 0.9, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.novelty_threshold_response", float, 0.8, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.pressure_threshold", float, 0.7, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.pressure_drop", float, 0.3, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.silence_threshold", float, 20.0, range=(0.0, 3600.0)),
        ConfigSchema("curiosity_config.question_cooldown", float, 60.0, range=(0.0, 3600.0)),
        ConfigSchema("curiosity_config.weight_ignorance", float, 0.7, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.weight_novelty", float, 0.3, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.max_new_tokens", int, 8, range=(1, 100)),
        ConfigSchema("curiosity_config.base_temperature", float, 1.1, range=(0.1, 2.0)),
        ConfigSchema("curiosity_config.temperament_influence", float, 0.4, range=(0.0, 1.0)),
        ConfigSchema("curiosity_config.top_k", int, 30, range=(1, 100)),
        ConfigSchema("curiosity_config.enable_curiosity", bool, True),
        # cross_attn_config
        ConfigSchema("cross_attn_config.memory_weight", float, 0.5, range=(0.0, 1.0)),
        ConfigSchema("cross_attn_config.dynamic_scale", float, 0.3, range=(0.0, 1.0)),
        ConfigSchema("cross_attn_config.enable_dynamic", bool, True),
        ConfigSchema("cross_attn_config.enable_memory", bool, True),
        # controls_config
        ConfigSchema("controls_config.sleep_conf_threshold", float, 0.7, range=(0.0, 1.0)),
        ConfigSchema("controls_config.sleep_time_factor", float, 1.0, range=(0.1, 10.0)),
        ConfigSchema("controls_config.sleep_log_min", int, 10, range=(1, 100)),
        ConfigSchema("controls_config.dream_swing_var", float, 0.1, range=(0.0, 0.5)),
        ConfigSchema("controls_config.dream_lifecycle_delta", float, 0.1, range=(0.0, 0.5)),
        ConfigSchema("controls_config.dream_temperament_on", bool, True),
        ConfigSchema("controls_config.dream_noise_scale", float, 0.05, range=(0.0, 0.1)),
        ConfigSchema("controls_config.temp_eager_threshold", float, 0.8, range=(0.7, 0.9)),
        ConfigSchema("controls_config.temp_sluggish_threshold", float, 0.6, range=(0.4, 0.6)),
        ConfigSchema("controls_config.temp_mood_influence", float, 0.0, range=(0.0, 1.0)),
        ConfigSchema("controls_config.scaffold_weight_cap", float, 0.9, range=(0.0, 1.0)),
        ConfigSchema("controls_config.base_temperature", float, 0.7, range=(0.1, 2.0)),
        ConfigSchema("controls_config.save_path_prefix", str, "state", lambda x: bool(re.match(r'^[a-zA-Z0-9_/.-]+$', x))),
        ConfigSchema("controls_config.dream_memory_weight", float, 0.1, range=(0.0, 1.0)),
        ConfigSchema("controls_config.dream_memory_maxlen", int, 10, range=(1, 50)),
        ConfigSchema("controls_config.dream_prompt_weight", float, 0.5, range=(0.0, 1.0)),
        ConfigSchema("controls_config.dream_novelty_boost", float, 0.03, range=(0.0, 0.1)),
        ConfigSchema("controls_config.temp_curiosity_boost", float, 0.5, range=(0.0, 0.5)),
        ConfigSchema("controls_config.temp_restless_drop", float, 0.1, range=(0.0, 0.5)),
        ConfigSchema("controls_config.temp_melancholy_noise", float, 0.02, range=(0.0, 0.05)),
        ConfigSchema("controls_config.conf_feedback_strength", float, 0.5, range=(0.0, 1.0)),
        ConfigSchema("controls_config.temp_smoothing_factor", float, 0.0, range=(0.0, 1.0)),
        ConfigSchema("controls_config.dream_memory_decay", float, 0.95, range=(0.0, 1.0)),
        ConfigSchema("controls_config.dream_prune_threshold", float, 0.1, range=(0.0, 0.5)),
        ConfigSchema("controls_config.use_scaffold_memory", bool, True),
        ConfigSchema("controls_config.use_token_map_memory", bool, True),
        ConfigSchema("controls_config.memory_decay_rate", float, 0.95, range=(0.0, 1.0)),
        ConfigSchema("controls_config.dynamic_cross_attn_mode", str, None, lambda x: x is None or x in ["adaptive", "fixed"], nullable=True),
        ConfigSchema("controls_config.has_woken", bool, False),
        ConfigSchema("controls_config.is_sleeping", bool, False),
        ConfigSchema("controls_config.confidence_history_maxlen", int, 5, range=(3, 10)),
        ConfigSchema("controls_config.temperament_history_maxlen", int, 5, range=(3, 10)),
        ConfigSchema("controls_config.conversation_history_maxlen", int, 10, range=(5, 50)),
        ConfigSchema("controls_config.max_seen_prompts", int, 1000, range=(100, 10000)),
        ConfigSchema("controls_config.prompt_timeout", float, 86400.0, range=(3600.0, 604800.0)),
        ConfigSchema("controls_config.temperament_decay_rate", float, 0.95, range=(0.0, 1.0)),
        ConfigSchema("controls_config.scaffold_unk_id", int, 0, range=(0, 100000)),
        ConfigSchema("controls_config.enable_dreaming", bool, True),
        ConfigSchema("controls_config.enable_temperament", bool, True),
        ConfigSchema("controls_config.enable_confidence_tracking", bool, True),
        ConfigSchema("controls_config.enable_gestation", bool, True),
        ConfigSchema("controls_config.enable_sleep_training", bool, True),
        ConfigSchema("controls_config.enable_cross_attention", bool, True),
        ConfigSchema("controls_config.enable_dynamic_cross_attention", bool, True),
        ConfigSchema("controls_config.enable_lora_adapters", bool, True),
        ConfigSchema("controls_config.enable_repetition_check", bool, True),
        ConfigSchema("controls_config.enable_prompt_driven_dreams", bool, True),
        ConfigSchema("controls_config.enable_lifecycle_weighting", bool, True),
        ConfigSchema("controls_config.memory_threshold", float, 0.85, range=(0.0, 1.0)),
        ConfigSchema("controls_config.enable_error_listening", bool, True),
        ConfigSchema("controls_config.enable_scaffold", bool, True),
        ConfigSchema("controls_config.injection_strategy", str, "sequential", lambda x: x in ["sequential", "parallel", "replace"]),
        # logging_config
        ConfigSchema("logging_config.log_dir", str, "logs"),
        ConfigSchema("logging_config.log_file", str, "sovl_logs.jsonl"),
        ConfigSchema("logging_config.debug_log_file", str, "sovl_debug.log"),
        ConfigSchema("logging_config.max_size_mb", int, 10, range=(0, 100)),
        ConfigSchema("logging_config.compress_old", bool, False),
        ConfigSchema("logging_config.max_in_memory_logs", int, 1000, range=(100, 10000)),
        ConfigSchema("logging_config.schema_version", str, "1.1"),
    ]

    def __init__(self, config_file: str, logger: Logger):
        """
        Initialize ConfigManager with configuration file path and logger.

        Args:
            config_file: Path to configuration file
            logger: Logger instance for recording events
        """
        self.config_file = os.getenv("SOVL_CONFIG_FILE", config_file)
        self.logger = logger
        self.store = _ConfigStore()
        self.validator = _SchemaValidator(logger)
        self.file_handler = _FileHandler(self.config_file, logger)
        self.lock = Lock()
        self._frozen = False
        self._last_config_hash = ""
        self._subscribers = set()  # Set of callbacks to notify on config changes
        self.validator.register(self.DEFAULT_SCHEMA)
        self._initialize_config()

    def _initialize_config(self):
        """Initialize configuration by loading and validating."""
        with self.lock:
            self.store.flat_config = self.file_handler.load()
            self._validate_and_set_defaults()
            self.store.rebuild_structured(self.DEFAULT_SCHEMA)
            self.store.update_cache(self.DEFAULT_SCHEMA)
            self._last_config_hash = self._compute_config_hash()
            self.logger.record_event(
                event_type="config_load",
                message=f"Loaded config from {self.config_file}",
                level="info",
                additional_info={
                    "config_file": self.config_file,
                    "config_hash": self._last_config_hash
                }
            )

    def _compute_config_hash(self) -> str:
        """Compute a hash of the current config for change tracking."""
        try:
            config_str = json.dumps(self.store.flat_config, sort_keys=True)
            return hashlib.sha256(config_str.encode()).hexdigest()[:16]
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message="Config hash computation failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return ""

    def _validate_and_set_defaults(self):
        """Validate entire configuration and set defaults where needed."""
        for schema in self.DEFAULT_SCHEMA:
            value = self.store.get_value(schema.field, schema.default)
            is_valid, corrected_value = self.validator.validate(schema.field, value)
            if not is_valid:
                self.store.set_value(schema.field, corrected_value)
                self.logger.record_event(
                    event_type="config_validation",
                    message=f"Set default value for {schema.field}",
                    level="warning",
                    additional_info={
                        "field": schema.field,
                        "default_value": corrected_value
                    }
                )

    def freeze(self):
        """Prevent further updates to the configuration."""
        with self.lock:
            self._frozen = True
            self.logger.record_event(
                event_type="config_frozen",
                message="Configuration frozen",
                level="info",
                additional_info={
                    "timestamp": time.time()
                }
            )

    def unfreeze(self):
        """Allow updates to the configuration."""
        with self.lock:
            self._frozen = False
            self.logger.record_event(
                event_type="config_unfrozen",
                message="Configuration unfrozen",
                level="info",
                additional_info={
                    "timestamp": time.time()
                }
            )

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve a value from the configuration using a dot-separated key.

        Args:
            key: Dot-separated configuration key
            default: Default value if key is missing

        Returns:
            Configuration value or default
        """
        with self.lock:
            value = self.store.get_value(key, default)
            if value == {} or value is None:
                self.logger.record_event(
                    event_type="config_warning",
                    message=f"Key '{key}' is empty or missing. Using default: {default}",
                    level="warning",
                    additional_info={
                        "key": key,
                        "default_value": default
                    }
                )
                return default
            return value

    def validate_keys(self, required_keys: List[str]):
        """
        Validate that all required keys exist in the configuration.

        Args:
            required_keys: List of required configuration keys

        Raises:
            ValueError: If any required keys are missing
        """
        with self.lock:
            missing_keys = [key for key in required_keys if self.get(key, None) is None]
            if missing_keys:
                self.logger.record_event(
                    event_type="config_error",
                    message=f"Missing required configuration keys: {', '.join(missing_keys)}",
                    level="error",
                    additional_info={
                        "keys": missing_keys
                    }
                )
                raise ValueError(f"Missing required configuration keys: {', '.join(missing_keys)}")

    def get_section(self, section: str) -> Dict[str, Any]:
        """
        Get entire configuration section as dict.

        Args:
            section: Configuration section name

        Returns:
            Dictionary of section key-value pairs
        """
        with self.lock:
            return self.store.get_section(section)

    def update(self, key: str, value: Any) -> bool:
        """Update a configuration value with validation."""
        try:
            with self.lock:
                if self._frozen:
                    self.logger.record_event(
                        event_type="config_error",
                        message="Cannot update: configuration is frozen",
                        level="error",
                        additional_info={"key": key}
                    )
                    return False

                is_valid, corrected_value = self.validator.validate(key, value)
                if not is_valid:
                    return False

                old_hash = self._last_config_hash
                self.store.set_value(key, value)
                self._last_config_hash = self._compute_config_hash()
                self.logger.record_event(
                    event_type="config_update",
                    message=f"Updated configuration key: {key}",
                    level="info",
                    additional_info={
                        "key": key,
                        "value": value,
                        "old_hash": old_hash,
                        "new_hash": self._last_config_hash
                    }
                )
                return True
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message=f"Failed to update config key {key}",
                level="error",
                additional_info={
                    "key": key,
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return False

    def subscribe(self, callback: Callable[[], None]) -> None:
        """
        Subscribe to configuration changes.
        
        Args:
            callback: Function to call when configuration changes
        """
        with self.lock:
            self._subscribers.add(callback)

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        """
        Unsubscribe from configuration changes.
        
        Args:
            callback: Function to remove from subscribers
        """
        with self.lock:
            self._subscribers.discard(callback)

    def _notify_subscribers(self) -> None:
        """Notify all subscribers of configuration changes."""
        with self.lock:
            for callback in self._subscribers:
                try:
                    callback()
                except Exception as e:
                    self.logger.record_event(
                        event_type="config_notification_error",
                        message="Failed to notify subscriber of config change",
                        level="error",
                        additional_info={
                            "error": str(e),
                            "stack_trace": traceback.format_exc()
                        }
                    )

    def update_batch(self, updates: Dict[str, Any], rollback_on_failure: bool = True) -> bool:
        """Update multiple configuration values in a transactional manner.
        
        Args:
            updates: Dictionary of key-value pairs to update
            rollback_on_failure: Whether to rollback changes if any update fails
            
        Returns:
            bool: True if all updates succeeded, False otherwise
        """
        if not updates:
            return True
            
        # Create backup of current config
        backup = self.store.flat_config.copy()
        successful_updates = {}
        
        try:
            # First validate all updates
            for key, value in updates.items():
                if not self.validator.validate(key, value):
                    raise ValueError(f"Invalid configuration key: {key}")
                    
                # Validate value against schema if exists
                section, param = key.split(".", 1)
                if section in self.validator.schemas and param in self.validator.schemas[section]:
                    schema = self.validator.schemas[section][param]
                    if not schema.validate(value):
                        raise ValueError(f"Invalid value for {key}: {value}")
            
            # Apply updates
            for key, value in updates.items():
                self.store.set_value(key, value)
                successful_updates[key] = value
                
            # Save changes
            if not self.file_handler.save(self.store.flat_config):
                raise RuntimeError("Failed to save configuration")
                
            # Notify subscribers
            self._notify_subscribers()
            
            return True
            
        except Exception as e:
            # Rollback changes if enabled
            if rollback_on_failure:
                self.store.flat_config = backup
                self.store.rebuild_structured(self.DEFAULT_SCHEMA)
                self.store.update_cache(self.DEFAULT_SCHEMA)
                self._last_config_hash = self._compute_config_hash()
                self.logger.record_event(
                    event_type="config_rollback",
                    message=f"Configuration rollback triggered: {str(e)}",
                    level="error",
                    additional_info={
                        "failed_updates": list(updates.keys()),
                        "successful_updates": list(successful_updates.keys())
                    }
                )
            else:
                self.logger.record_event(
                    event_type="config_update_failed",
                    message=f"Configuration update failed: {str(e)}",
                    level="error",
                    additional_info={
                        "failed_updates": list(updates.keys()),
                        "successful_updates": list(successful_updates.keys())
                    }
                )
            return False

    def save_config(self, file_path: Optional[str] = None, compress: bool = False, max_retries: int = 3) -> bool:
        """
        Save current configuration to file atomically.

        Args:
            file_path: Optional path to save config (defaults to config_file)
            compress: Save as gzip-compressed file
            max_retries: Number of retry attempts for I/O operations

        Returns:
            True if save succeeded, False otherwise
        """
        with self.lock:
            return self.file_handler.save(self.store.flat_config, file_path, compress, max_retries)

    def diff_config(self, old_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        Compare current config with an old config and return differences.

        Args:
            old_config: Previous configuration dictionary

        Returns:
            Dictionary of changed keys with old and new values
        """
        try:
            with self.lock:
                diff = {}
                for key in self.store.flat_config:
                    old_value = old_config.get(key)
                    new_value = self.store.flat_config.get(key)
                    if old_value != new_value:
                        diff[key] = {"old": old_value, "new": new_value}
                for key in old_config:
                    if key not in self.store.flat_config:
                        diff[key] = {"old": old_config[key], "new": None}
                self.logger.record_event(
                    event_type="config_diff",
                    message="Configuration differences",
                    level="info",
                    additional_info={
                        "changed_keys": list(diff.keys()),
                        "differences": diff
                    }
                )
                return diff
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message="Config diff failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return {}

    def register_schema(self, schemas: List[ConfigSchema]):
        """
        Dynamically extend the configuration schema.

        Args:
            schemas: List of ConfigSchema objects to add
        """
        try:
            with self.lock:
                if self._frozen:
                    self.logger.record_event(
                        event_type="config_error",
                        message="Cannot register schema: configuration is frozen",
                        level="error",
                        additional_info={}
                    )
                    return
                self.validator.register(schemas)
                self._validate_and_set_defaults()
                self.store.rebuild_structured(self.DEFAULT_SCHEMA + schemas)
                self.store.update_cache(self.DEFAULT_SCHEMA + schemas)
                self._last_config_hash = self._compute_config_hash()
                self.logger.record_event(
                    event_type="schema_registered",
                    message=f"New fields registered: {', '.join([s.field for s in schemas])}",
                    level="info",
                    additional_info={
                        "new_fields": [s.field for s in schemas],
                        "config_hash": self._last_config_hash
                    }
                )
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message="Failed to register schema",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )

    def get_state(self) -> Dict[str, Any]:
        """
        Export current configuration state.

        Returns:
            Dictionary containing config state
        """
        with self.lock:
            return {
                "config_file": self.config_file,
                "config": self.store.flat_config,
                "frozen": self._frozen,
                "config_hash": self._last_config_hash
            }

    def load_state(self, state: Dict[str, Any]) -> None:
        """
        Load configuration state.

        Args:
            state: Dictionary containing config state
        """
        try:
            with self.lock:
                self.config_file = state.get("config_file", self.config_file)
                self.store.flat_config = state.get("config", {})
                self._frozen = state.get("frozen", False)
                self._validate_and_set_defaults()
                self.store.rebuild_structured(self.DEFAULT_SCHEMA)
                self.store.update_cache(self.DEFAULT_SCHEMA)
                self._last_config_hash = self._compute_config_hash()
                self.logger.record_event(
                    event_type="config_load_state",
                    message="Configuration state loaded",
                    level="info",
                    additional_info={
                        "config_file": self.config_file,
                        "config_hash": self._last_config_hash
                    }
                )
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message="Failed to load config state",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

    def tune(self, **kwargs) -> bool:
        """
        Dynamically tune configuration parameters.

        Args:
            **kwargs: Parameters to update

        Returns:
            True if tuning succeeded, False otherwise
        """
        return self.update_batch(kwargs)

    def load_profile(self, profile: str) -> bool:
        """
        Load a configuration profile from a file.

        Args:
            profile: Profile name (e.g., 'development', 'production')

        Returns:
            True if profile loaded successfully, False otherwise
        """
        profile_file = f"{os.path.splitext(self.config_file)[0]}_{profile}.json"
        try:
            with self.lock:
                config = self.file_handler.load()
                if not config:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Profile file {profile_file} not found",
                        level="error",
                        additional_info={
                            "profile_file": profile_file
                        }
                    )
                    return False
                self.store.flat_config = config
                self._validate_and_set_defaults()
                self.store.rebuild_structured(self.DEFAULT_SCHEMA)
                self.store.update_cache(self.DEFAULT_SCHEMA)
                self._last_config_hash = self._compute_config_hash()
                self.logger.record_event(
                    event_type="profile_load",
                    message=f"Profile {profile} loaded",
                    level="info",
                    additional_info={
                        "profile": profile,
                        "config_file": profile_file,
                        "config_hash": self._last_config_hash
                    }
                )
                return True
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message=f"Failed to load profile {profile}",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return False

    def set_global_blend(self, weight_cap: Optional[float] = None, base_temp: Optional[float] = None) -> bool:
        """
        Set global blend parameters for the system.

        Args:
            weight_cap: Scaffold weight cap (0.5 to 1.0)
            base_temp: Base temperature (0.5 to 1.5)

        Returns:
            True if update succeeded, False otherwise
        """
        updates = {}
        prefix = "controls_config."
        
        if weight_cap is not None and 0.5 <= weight_cap <= 1.0:
            updates[f"{prefix}scaffold_weight_cap"] = weight_cap
            
        if base_temp is not None and 0.5 <= base_temp <= 1.5:
            updates[f"{prefix}base_temperature"] = base_temp
            
        if updates:
            return self.update_batch(updates)
        return True

    def validate_section(self, section: str, required_keys: List[str]) -> bool:
        """
        Validate a configuration section and its required keys.

        Args:
            section: Configuration section name
            required_keys: List of required keys in the section

        Returns:
            True if section is valid, False otherwise
        """
        try:
            with self.lock:
                # Check if section exists
                if section not in self.store.structured_config:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Configuration section '{section}' not found",
                        level="error",
                        additional_info={
                            "section": section
                        }
                    )
                    return False

                # Check for required keys
                missing_keys = [key for key in required_keys if key not in self.store.structured_config[section]]
                if missing_keys:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Missing required keys in section '{section}': {', '.join(missing_keys)}",
                        level="error",
                        additional_info={
                            "section": section,
                            "missing_keys": missing_keys
                        }
                    )
                    return False

                return True
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message=f"Failed to validate section '{section}'",
                level="error",
                additional_info={
                    "section": section,
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return False

    def tune_parameter(self, section: str, key: str, value: Any, min_value: Any = None, max_value: Any = None) -> bool:
        """
        Tune a configuration parameter with validation.

        Args:
            section: Configuration section name
            key: Parameter key
            value: New parameter value
            min_value: Minimum allowed value
            max_value: Maximum allowed value

        Returns:
            True if parameter was updated successfully, False otherwise
        """
        try:
            with self.lock:
                # Validate value range if min/max provided
                if min_value is not None and value < min_value:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Value {value} below minimum {min_value} for {section}.{key}",
                        level="error",
                        additional_info={
                            "section": section,
                            "key": key,
                            "value": value,
                            "min_value": min_value
                        }
                    )
                    return False
                    
                if max_value is not None and value > max_value:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Value {value} above maximum {max_value} for {section}.{key}",
                        level="error",
                        additional_info={
                            "section": section,
                            "key": key,
                            "value": value,
                            "max_value": max_value
                        }
                    )
                    return False

                # Update the parameter
                success = self.update(section, key, value)
                if success:
                    self.logger.record_event(
                        event_type="config_info",
                        message=f"Tuned {section}.{key} to {value}",
                        level="info",
                        additional_info={
                            "section": section,
                            "key": key,
                            "value": value
                        }
                    )
                return success
        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message=f"Failed to tune {section}.{key}",
                level="error",
                additional_info={
                    "section": section,
                    "key": key,
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return False

    def update_section(self, section: str, updates: Dict[str, Any]) -> bool:
        """
        Update a configuration section with new values.

        Args:
            section: Configuration section name
            updates: Dictionary of key-value pairs to update

        Returns:
            True if update successful, False otherwise
        """
        try:
            with self.lock:
                # Check if section exists
                if section not in self.store.structured_config:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Configuration section '{section}' not found",
                        level="error",
                        additional_info={
                            "section": section
                        }
                    )
                    return False

                # Update values
                for key, value in updates.items():
                    if key in self.store.structured_config[section]:
                        self.store.structured_config[section][key] = value
                        self.logger.record_event(
                            event_type="config_update",
                            message=f"Updated {section}.{key}",
                            level="info",
                            additional_info={
                                "section": section,
                                "key": key,
                                "value": str(value)
                            }
                        )

                # Save changes
                self.save_config()
                return True

        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message=f"Failed to update section '{section}'",
                level="error",
                additional_info={
                    "section": section,
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return False

    def validate_or_raise(self, model_config: Optional[Any] = None) -> None:
        """Validate the entire configuration and raise a ValueError with detailed error messages if validation fails."""
        try:
            # Validate required keys
            self.validate_keys([
                "core_config.base_model_name",
                "core_config.scaffold_model_name",
                "training_config.learning_rate",
                "curiosity_config.enable_curiosity",
                "cross_attn_config.memory_weight"
            ])

            # Validate cross-attention layers
            cross_attn_layers = self.get("core_config.cross_attn_layers", [5, 7])
            if not isinstance(cross_attn_layers, list):
                raise ValueError("core_config.cross_attn_layers must be a list")

            # Validate layer indices if not using dynamic layers
            if not self.get("core_config.use_dynamic_layers", False) and model_config is not None:
                base_model_name = self.get("core_config.base_model_name", "gpt2")
                try:
                    base_config = model_config or AutoConfig.from_pretrained(base_model_name)
                    invalid_layers = [l for l in cross_attn_layers if not (0 <= l < base_config.num_hidden_layers)]
                    if invalid_layers:
                        raise ValueError(f"Invalid cross_attn_layers: {invalid_layers} for {base_config.num_hidden_layers} layers")
                except Exception as e:
                    raise ValueError(f"Failed to validate cross-attention layers: {str(e)}")

            # Validate custom layers if using custom layer selection
            if self.get("core_config.layer_selection_mode", "balanced") == "custom":
                custom_layers = self.get("core_config.custom_layers", [])
                if not isinstance(custom_layers, list):
                    raise ValueError("core_config.custom_layers must be a list")

                if model_config is not None:
                    try:
                        base_model_name = self.get("core_config.base_model_name", "gpt2")
                        base_config = model_config or AutoConfig.from_pretrained(base_model_name)
                        invalid_custom = [l for l in custom_layers if not (0 <= l < base_config.num_hidden_layers)]
                        if invalid_custom:
                            raise ValueError(f"Invalid custom_layers: {invalid_custom} for {base_model_name}")
                    except Exception as e:
                        raise ValueError(f"Failed to validate custom layers: {str(e)}")

            # Log successful validation
            self.logger.record_event(
                event_type="config_validation",
                message="Configuration validation successful",
                level="info",
                additional_info={
                    "config_snapshot": self.get_state()["config"]
                }
            )

        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message="Configuration validation failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise ValueError(f"Configuration validation failed: {str(e)}")

    def validate_value(self, key: str, value: Any) -> bool:
        """
        Validate a configuration value against its schema.
        
        Args:
            key: Dot-separated configuration key
            value: Value to validate
            
        Returns:
            bool: True if value is valid, False otherwise
        """
        try:
            with self.lock:
                # Check if key exists in schema
                schema = next((s for s in self.DEFAULT_SCHEMA if s.field == key), None)
                if not schema:
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Unknown configuration key: {key}",
                        level="error",
                        additional_info={"key": key}
                    )
                    return False

                # Validate value type
                if not isinstance(value, schema.type):
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Invalid type for {key}: expected {schema.type.__name__}, got {type(value).__name__}",
                        level="error",
                        additional_info={
                            "key": key,
                            "expected_type": schema.type.__name__,
                            "actual_type": type(value).__name__
                        }
                    )
                    return False

                # Validate value range if specified
                if schema.range and isinstance(value, (int, float)):
                    min_val, max_val = schema.range
                    if not (min_val <= value <= max_val):
                        self.logger.record_event(
                            event_type="config_error",
                            message=f"Value for {key} outside valid range [{min_val}, {max_val}]: {value}",
                            level="error",
                            additional_info={
                                "key": key,
                                "value": value,
                                "min_val": min_val,
                                "max_val": max_val
                            }
                        )
                        return False

                # Validate with custom validator if specified
                if schema.validator and not schema.validator(value):
                    self.logger.record_event(
                        event_type="config_error",
                        message=f"Value for {key} failed custom validation: {value}",
                        level="error",
                        additional_info={
                            "key": key,
                            "value": value
                        }
                    )
                    return False

                return True

        except Exception as e:
            self.logger.record_event(
                event_type="config_error",
                message=f"Failed to validate value for {key}",
                level="error",
                additional_info={
                    "key": key,
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            return False

class ConfigHandler:
    """Handles configuration validation and management."""
    
    def __init__(self, config_path: str, logger: Logger, event_dispatcher: EventDispatcher):
        """
        Initialize config handler with explicit dependencies.
        
        Args:
            config_path: Path to configuration file
            logger: Logger instance for logging events
            event_dispatcher: Event dispatcher for handling events
        """
        self.logger = logger
        self.event_dispatcher = event_dispatcher
        self.config_manager = ConfigManager(config_path, logger)
        self.config_manager.set_event_dispatcher(event_dispatcher)
        
        # Subscribe to configuration changes
        self.event_dispatcher.subscribe("config_change", self._on_config_change)
        self._refresh_configs()
        
    def _on_config_change(self) -> None:
        """Handle configuration changes."""
        try:
            # Refresh configurations
            self._refresh_configs()
            
            # Validate configurations
            warnings = self._validate_all_configs()
            if warnings:
                self.logger.record_event(
                    event_type="config_validation_warnings",
                    message="Configuration validation warnings",
                    level="warning",
                    additional_info={"warnings": warnings}
                )
                
            # Notify other components
            self.event_dispatcher.notify("config_validated", warnings)
            
        except Exception as e:
            self.logger.record_event(
                event_type="config_change_error",
                message=f"Failed to handle config change: {str(e)}",
                level="error",
                additional_info={"error": str(e)}
            )
            
    def _refresh_configs(self) -> None:
        """Refresh configuration sections from ConfigManager."""
        self.core_config = self.config_manager.get_section("core_config")
        self.controls_config = self.config_manager.get_section("controls_config")
        self.curiosity_config = self.config_manager.get_section("curiosity_config")
        self.training_config = self.config_manager.get_section("training_config")
        
        self.logger.record_event(
            event_type="config_refresh",
            message="Configuration sections refreshed",
            level="info"
        )
        
    def _validate_all_configs(self) -> List[str]:
        """
        Validate all configuration sections.
        
        Returns:
            List of warning messages, empty if no warnings
        """
        warnings = []
        
        # Define validation sections and their specific validation rules
        validation_sections = [
            ("controls_config", self.controls_config, {}),
            ("curiosity_config", self.curiosity_config, {}),
            ("core_config", self.core_config, {
                "processor": lambda k, v: k.startswith("processor_"),
                "temperament": lambda k, v: k.startswith("temp_")
            })
        ]
        
        # Validate each section
        for section_name, config, filters in validation_sections:
            section_warnings = self._validate_config_section(
                section_name=section_name,
                config=config,
                filters=filters
            )
            warnings.extend(section_warnings)
            
        return warnings
        
    def _validate_config_section(
        self,
        section_name: str,
        config: Dict[str, Any],
        filters: Dict[str, Callable[[str, Any], bool]] = None
    ) -> List[str]:
        """
        Validate a configuration section.
        
        Args:
            section_name: Name of the configuration section
            config: Configuration dictionary to validate
            filters: Optional dictionary of filter functions for specific validation rules
            
        Returns:
            List of warning messages, empty if no warnings
        """
        warnings = []
        
        try:
            for key, value in config.items():
                # Apply filters if specified
                if filters:
                    for filter_name, filter_func in filters.items():
                        if filter_func(key, value):
                            # Skip validation for filtered keys
                            continue
                
                # Validate the value
                is_valid, error_msg = ValidationSchema.validate_value(
                    section_name, key, value, self.logger
                )
                
                if not is_valid:
                    warnings.append(f"{section_name}.{key}: {error_msg}")
                    self.logger.record_event(
                        event_type="config_validation_error",
                        message=f"Invalid {section_name} config value: {error_msg}",
                        level="error",
                        additional_info={
                            "key": key,
                            "value": value
                        }
                    )
                    
        except Exception as e:
            self.logger.record_event(
                event_type="config_validation_error",
                message=f"Failed to validate {section_name} config: {str(e)}",
                level="error",
                additional_info={
                    "section": section_name,
                    "error": str(e)
                }
            )
            warnings.append(f"Failed to validate {section_name} config: {str(e)}")
            
        return warnings
        
    def validate(self, model_config: Any = None) -> bool:
        """
        Validate all configurations.
        
        Args:
            model_config: Optional model configuration for additional validation
            
        Returns:
            bool: True if validation succeeds, False otherwise
        """
        try:
            warnings = self._validate_all_configs()
            if warnings:
                self.logger.record_event(
                    event_type="config_validation_failed",
                    message="Configuration validation failed with warnings",
                    level="error",
                    additional_info={"warnings": warnings}
                )
                return False
            return True
        except Exception as e:
            self.logger.record_event(
                event_type="config_validation_failed",
                message=f"Configuration validation failed: {str(e)}",
                level="error"
            )
            return False
            
    def validate_with_model(self, model_config: Any) -> bool:
        """
        Validate configurations with model-specific checks.
        
        Args:
            model_config: Model configuration for additional validation
            
        Returns:
            bool: True if validation succeeds, False otherwise
        """
        try:
            # First validate basic configurations
            if not self.validate():
                return False
                
            # Add model-specific validation here if needed
            return True
        except Exception as e:
            self.logger.record_event(
                event_type="config_validation_failed",
                message=f"Configuration validation failed: {str(e)}",
                level="error"
            )
            return False

if __name__ == "__main__":
    from sovl_logger import LoggerConfig
    logger = Logger(LoggerConfig())
    config_manager = ConfigManager("sovl_config.json", logger)
    try:
        config_manager.validate_keys(["core_config.base_model_name", "curiosity_config.attention_weight"])
    except ValueError as e:
        print(e)
