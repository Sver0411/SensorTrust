# The core is plain C and lives outside the firmware directory; compile it into
# the main component so there is exactly one implementation of the detectors.
idf_component_register(
    SRCS "main.c" "${CMAKE_CURRENT_LIST_DIR}/../../core/sensor_trust.c"
    INCLUDE_DIRS "." "${CMAKE_CURRENT_LIST_DIR}/../../core"
)
