set(ROOT_DIR "${CMAKE_CURRENT_LIST_DIR}/../..")
set(CONFIG_JSON "${CMAKE_CURRENT_LIST_DIR}/../experiment_config.json")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CONFIG_JSON}")
find_package(Python3 REQUIRED COMPONENTS Interpreter)
file(SHA256 "${CONFIG_JSON}" CONFIG_SHA256)
execute_process(
    COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_LIST_DIR}/../generate_config.py" "${CONFIG_JSON}"
    OUTPUT_FILE "${CMAKE_CURRENT_BINARY_DIR}/experiment_config.h"
    RESULT_VARIABLE CONFIG_RESULT
)
if(NOT CONFIG_RESULT EQUAL 0)
    message(FATAL_ERROR "experiment config generation failed")
endif()
execute_process(COMMAND git rev-parse HEAD WORKING_DIRECTORY "${ROOT_DIR}"
                OUTPUT_VARIABLE GIT_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE)
execute_process(COMMAND git status --porcelain --untracked-files=all
                WORKING_DIRECTORY "${ROOT_DIR}" OUTPUT_VARIABLE GIT_STATUS)
if(GIT_STATUS STREQUAL "")
    set(GIT_DIRTY 0)
else()
    set(GIT_DIRTY 1)
endif()
idf_component_register(
    SRCS "main.c" "sensor_reader.c" "fault_injector.c" "experiment.c"
         "${ROOT_DIR}/core/sensor_trust.c"
    INCLUDE_DIRS "." "${ROOT_DIR}/core" "${CMAKE_CURRENT_BINARY_DIR}"
    REQUIRES esp_driver_i2c esp_timer esp_system spi_flash heap
)
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    ST_CONFIG_SHA256="${CONFIG_SHA256}"
    ST_GIT_COMMIT="${GIT_COMMIT}"
    ST_GIT_DIRTY=${GIT_DIRTY}
)
