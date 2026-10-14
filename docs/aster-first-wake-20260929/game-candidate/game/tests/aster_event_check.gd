extends "res://tests/test_runner.gd"

func _discover_test_files() -> PackedStringArray:
	return PackedStringArray([
		"test_companion_client.gd",
		"test_companion_protocol.gd",
		"test_lilt_encounter_integration.gd",
		"test_station_signal_state.gd",
		"test_station_signal_lifetime.gd",
		"test_bench_assay.gd",
		"test_field_discovery_state.gd",
		"test_field_inventory_panel.gd",
		"test_station_signal_knowledge.gd",
		"test_station_signal_modals.gd",
		"test_companion_context_provider.gd",
		"test_companion_save_context.gd",
		"test_discovery_review_guide.gd",
		"test_pocket_receiver_controller.gd",
	])
