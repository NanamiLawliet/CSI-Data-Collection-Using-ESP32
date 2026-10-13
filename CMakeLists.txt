# For more information about build system see
# https://docs.espressif.com/projects/esp-idf/en/latest/api-guides/build-system.html
# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.5)

set(CMAKE_C_ARCHIVE_FINISH "<CMAKE_AR> s <TARGET>")
set(CMAKE_CXX_ARCHIVE_FINISH "<CMAKE_AR> s <TARGET>")
set(CMAKE_ASM_ARCHIVE_FINISH "<CMAKE_AR> s <TARGET>")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(project-name)