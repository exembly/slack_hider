# Authors: Zachary Parish, Aaron Lam, Omid Novtash
# This module provides classes to handle interaction with the drives
# It is assumed that this program is running on Linux, and that debugfs is installed.
# This code has been tested on Arch Linux, but should run on other distros

import subprocess
import os
from datetime import datetime

# This needs to be cleaned up for sep of concerns
# The location map should be an object that contains locations but also:
#       1. crypto support
#       2. multiple location blocks
#       4. checksums for the passed or created blob
#       5. The ability to actually clear the content out of slack space
class drive_handler():
    def __init__(self, drive_mount_path, verbose=False, sector_size=512, block_size=4096):
        self.v = verbose  # If True, verbose output is printed during operation
        self.sector_size = sector_size  # The size of units we will allocate to in slack space (factor of block size)
        self.block_size = block_size  # The block size (cluster size) of the drive

        # Where the target drive is mounted
        self.mount_path = drive_mount_path
        print('Mount point for drive is:', self.mount_path)

        # The device mounted at the mount point
        self.device = self.get_device_path()
        print('Device is:', self.device)

        self.file_paths = []  # Paths to all files on the drive
        self.empty_space_chunks = []  # List of lists containing contiguous free sectors in slack space
        self.allocated_space_locations = []  # Sectors in slack space that are allocated to hidden files
        self.free_space = 0  # Total hidden space in all free regions
        self.allocated_space = 0  # Total allocated space
        self.free_space_chunk_sizes = []  # list of the sizes of free space chunks
        self.slack_table = None  # The slack table whose entry denote hidden files locations
        self.slack_table_dict = {}  # Convenience lookup table for slack table entries
        self.slack_table_location = None  # On-disk location of the slack table (should be first free slack sector)
        self.prepared = False  # Is the disk prepared for read/write

    # Use subprocess to return the path to the device from the mount path
    def get_device_path(self):
        return str(subprocess.check_output(['df', '-h', str(self.mount_path)])).split('Mounted on\\n')[1].split(' ')[0]

    # Load the SAT and map all slack sectors, also output information related to free and allocated space
    def prepare_hidden_storage(self):
        self.get_files()
        if self.v: print("File paths", self.file_paths)

        self.find_slack_space_locations()
        if self.v: print("Empty Space Locations", self.empty_space_chunks)
        if self.v: print("Allocated Space Locations", self.allocated_space_locations)

        self.sum_free_space()
        print("Free Space (Bytes):", self.free_space)
        print("Free Space (Sectors):", self.free_space / self.sector_size)
        self.prepared = True

        if self.v: print("Slack Table:", self.slack_table)

    # Locate file paths for all files on the target drive
    def get_files(self):
        files = []
        # walk the mount point of the target drive
        for path, directories_names, file_names in os.walk(self.mount_path):
            files_on_path = []
            # for each file on the current path, concatanate the file name to the path
            # and save the resulting absolute path
            for file_name in file_names:
                files_on_path.append(path+'/'+file_name)
            files.extend(files_on_path)
        self.file_paths = files  # set the file_paths variable to a list of all discovered file paths

    # This function does the work of locating all of the slack sectors on the disk and then divides them into
    #   allocated and unallocated lists based on the SAT. The unallocated slack sectors are further sudivided into
    #   contiguous ranges.
    def find_slack_space_locations(self):
        slack_space_ranges = []

        # Iterate over all file paths on the drive
        for file_path in self.file_paths:
            # Use os.stat to learn the file size and inode number of each file
            file_stat = os.stat(file_path)
            file_size = file_stat.st_size
            file_inode = file_stat.st_ino
            if self.v: print(file_path, file_size, file_inode)

            # Use debugfs via subprocess to learn the extents allocated to a file
            result = subprocess.run(['debugfs', '-R', 'stat <' +str(file_inode)+'>', self.device], capture_output=True, encoding='utf-8')

            # Split the result string based and select the section after "EXTENTS:". Further split the output
            #   by ":" and then by "\n" to remove blank lines and select only the value-value or value section of the
            #   output
            raw_extents = result.stdout.split('EXTENTS:')[1].split(":")[1].split('\n')
            raw_extents.remove('')

            # Select just the first value-value line or single value in the extents section
            # Note that this means we only use one cluster range for a fragmented file
            # THIS CODE WAS NOT TESTED WITH FRAGMENTED FILES AND MAY BE UNSTABLE WHEN PRESENTED WITH THEM
            extent = raw_extents[0]

            # Split the extent by the "-" token. If there is only one cluster allocated to the file, this will result
            #   in a single value being stored in the list, if there is a range of clusters, this will create a list
            #   with two values in it. We use a length check to determine if it is a single or multiple clusters.
            extent_split = extent.split('-')

            if len(extent_split) == 2:
                start = int(extent.split('-')[0])
                end = int(extent.split('-')[1]) + 1

            elif len(extent_split) == 1:
                start = int(extent)
                end = int(extent) + 1

            # This option should never be reached
            else:
                print("BAD VALUE FOR EXTENTS")
                return []

            # convert each extent into bytes offsets (bytes from start of drive to location of slack sector)
            start = (start * self.block_size)
            end = (end * self.block_size)

            # calculate the byte offset where the actual file content ends
            file_end = start + file_size

            # calculate the number of 512 byte slack sectors that can fit in this slack space
            free_sectors = (end - (file_end))//self.sector_size
            if self.v: print(file_path, 'has', free_sectors, 'free sectors')

            # Calculate the byte offset where our usable slack space starts, starting from the end of the cluster,
            # jump backwards by the number of slack sectors that will fit.
            free_start = end - (free_sectors * self.sector_size)

            # save the start of the slack space range and its end
            slack_space_ranges.append([free_start, end])

        # We next convert the ranges into individual byte offsets which represent the distance in bytes from the start
        #   of the disk to each 512 byte slack sector
        slack_space_locations = []

        # Iterate through all discovered ranges. For each range record the start offsets of each slack sectors
        for location_range in slack_space_ranges:
            for location in range(location_range[0], location_range[1], self.sector_size):
                slack_space_locations.append(location)

        self.all_slack_locations = sorted(slack_space_locations)

        # Select the first empty slack sector to be used to store the SAT
        self.slack_table_location = self.all_slack_locations.pop(0)

        # Now read the slack table if it exists. We assume that the drive was zeroed, without zeroing we risk
        #   loading nonsense values as our slack table as the program does not verify the slack table is legitimate
        print("Reading slack table from:", self.slack_table_location)

        # Open the disk and seek to the first slack sector
        drive = open(self.device, 'rb')
        drive.seek(self.slack_table_location)

        # read the 510 byte SAT
        raw_slack_table = drive.read(510)
        drive.close()

        # Parse each entry in the SAT. Each will be a start (8 bytes), end (8 bytes) and id (1 byte)
        #   Cast all of these values to integers
        #   Note that if an id is 0, we skip creating an entry. When the disk is zero, or when entries are removed and
        #   zeroed, their ids will appear to be zero.
        slack_table = []
        for entry_start in range(0, 510, 17):
            entry = raw_slack_table[entry_start:entry_start+17]
            start_loc = int.from_bytes(entry[:8], byteorder='big')
            end_loc = int.from_bytes(entry[8:16], byteorder='big')
            id = int.from_bytes(entry[16:], byteorder='big')

            if id == 0:
                continue

            slack_table.append([start_loc, end_loc, id])
        self.slack_table = slack_table

        # Create a dict where ids are keys and start and end addresses are values
        for entry in self.slack_table:
            self.slack_table_dict[entry[-1]] = entry[:2]

        # determine the addresses for slack sectors that are already allocated. Iterate through all locations and check
        #   if they fall between any start and end addresses for any entry in the SAT
        #   NOTE: This is not an optimized way to complete this task, but is sufficient for explanation purposes.
        used_locations = []
        for location in self.all_slack_locations:
            for entry in slack_table:
                if int(entry[0]) <= int(location) <= int(entry[1]):
                    used_locations.append(location)
                    break  # Break since a location can only fall within one range

        # Compute the chucks of slack sectors (slack sectors that have no allocated sectors between them
        free_chunks = []
        current_chunk = []
        # Iterate over all slack sector addresses
        for location in self.all_slack_locations:

            # if the address is not allocated add it to the list of addresses in the current run
            if location not in used_locations:
                current_chunk.append(location)

            # once an allocated address is encountered, append the current run to the list of all free chunks and
            #   start a new run
            else:
                # If we encounter multiple allocated addresses in a row, don't create a new chunk
                if current_chunk == []:
                    continue
                else:
                    free_chunks.append(current_chunk)
                    current_chunk = []

        # This last section ensures that the final chunk located will also be appended to the list of all chunks
        if current_chunk != []:
            free_chunks.append(current_chunk)

        # Set the allocated and unallocated object variables
        self.allocated_space_locations = used_locations
        self.empty_space_chunks = free_chunks

    # Sum the total free and allocated space as well as the free space in each chunk
    def sum_free_space(self):
        sizes = []
        for chunk in self.empty_space_chunks:
            sizes.append(len(chunk) * self.sector_size)
        self.free_space = sum(sizes)
        self.free_space_chunk_sizes = sizes
        self.allocated_space = len(self.allocated_space_locations) * self.sector_size

    # This function handles writing blobs into slack space and creating new SAT entries
    def write_blob_to_slack(self, blob, blob_id):
        # Make sure the drive is prepared
        if not self.prepared:
            print("Drive Handler Not Prepared")
            return None
        # Get the size of the blob and then find a chunk that is large enough to fit the blob by iterating over the
        #   sizes of each free chunk
        blob_size = len(blob)
        chunk_locations = None
        chunk_id = 0
        for chunk_size in self.free_space_chunk_sizes:
            if chunk_size >= blob_size:
                chunk_locations = self.empty_space_chunks[chunk_id]
                print("saving in chunk:", chunk_id)
            else:
                chunk_id += 1

        # If we found a large enough free chunk, report this information, otherwise report that no space is large
        #   enough and quit
        if chunk_locations:
            print("Hiding", blob_size, "bytes in", self.free_space_chunk_sizes[chunk_id], "bytes of slack space")
        else:
            print("FAILED: No free space chunk large enough for blob")
            quit()

        # Make sure that the id is valid. It should be in [1,255] and not already in use
        if not(1<=int(blob_id)<=255):
            print("Invalid ID: must be in [1, 255]")

        if blob_id in self.slack_table_dict:
            print("FAILED: ID already in use")

        # Split the blob into sections of 512 bytes and align these sections with particular slack sector addresses
        print("Preparing to write blob")
        location_map = []
        data_array = []

        for data_sector in range(0, len(blob), self.sector_size):
            location = chunk_locations.pop(0)
            data_array.append(blob[data_sector: data_sector + self.sector_size])
            location_map.append(location)

        # Create a new SAT entry
        # First check that the SAT is not full
        print("Updating Slack Table Entry")
        if len(self.slack_table) >= 30:
            print("FAILED: Slack Table is full")
            quit()

        # Add the new entry to the SAT based on the first and last location used
        self.slack_table.append([location_map[0], location_map[-1], blob_id])
        print("Slack Table Updated with id=", blob_id, "and locations from:", location_map[0], 'to', location_map[-1])
        print("Writing new slack table to disk")

        # Save the new slack table
        self.save_slack_table()

        print("Opening Target Drive")
        drive = open(self.device, 'wb')

        # Write the blob data sections to disk in the aligned slack sectors
        print("Writing Data to drive")
        for i in range(0, len(data_array)):
            drive.seek(location_map[i])
            drive.write(data_array[i])
            drive.flush()

        print("Closing Target Drive")
        drive.close()

    # Simply iterate over all discovered slack sectors (include the SAT location) and replace the data with zeros
    def purge_slack_space(self):
        drive = open(self.device, 'wb')

        drive.seek(self.slack_table_location)
        drive.write(bytes(self.sector_size))

        for location in self.all_slack_locations:
            drive.seek(location)
            drive.write(bytes(self.sector_size))
            drive.flush()
        drive.close()

    # Save the SAT
    def save_slack_table(self):
        print("saving slack table to:", self.slack_table_location)
        print(self.slack_table)
        slack_table_bytes = bytes()
        # For each entry in the SAT, cast the start, end and id to bytes, concatenate them and then concatenate
        #   the entry entry to the slack table bytes object
        for entry in self.slack_table:
            start = int(entry[0]).to_bytes(8, byteorder='big')
            end = int(entry[1]).to_bytes(8, byteorder='big')
            id = int(entry[2]).to_bytes(1, byteorder='big')
            slack_table_bytes += start+end+id

        # Open the drive, seek to the slack table address, overwrite the slack sector with zeros and then save
        #   The slack table bytes object at the slack table address
        #   NOTE: We zero the slack sector first to prevent the table from being corrupted in the case where
        #   The slack table being saved is smaller than a previous slack table
        if len(slack_table_bytes) <=510:
            drive = open(self.device, 'wb')
            drive.seek(self.slack_table_location)
            drive.write(bytes(512))
            drive.seek(self.slack_table_location)
            drive.write(slack_table_bytes)
            drive.close()
        else:
            print("Slack table too large")

    # This function retrieves a blob object with a particular blob id from slack space
    def retrieve_blob_from_slack(self, blob_id):

        # First load in all slack sector addresses
        location_map = self.allocated_space_locations

        # Slice the list of all addresses from the start address from the SAT entry for this blob to the end entry
        #   NOTE: We add 1 to the end address so that the slice will include the blob's end
        start_index = location_map.index(self.slack_table_dict[blob_id][0])
        end_index = location_map.index(self.slack_table_dict[blob_id][1]) + 1
        location_map = location_map[start_index:end_index]

        print("Opening Target Drive")
        drive = open(self.device, 'rb')

        # Create an empty bytes object, then seek to each address where the blob is allocated and read 512 bytes
        #   Concatenate the read bytes to the blob's data array
        data_array = bytes()
        print("Reading Data from drive")
        for location in location_map:
            drive.seek(location)
            data_array += (drive.read(self.sector_size))

        print("Closing Target Drive")
        drive.close()

        # Clear all of the slack sectors the file once used by zeroing them out
        #   NOTE: We do this for simplicity. In practice we might wish to keep data in slack space even after we read it
        print("Clearing Hidden data from disk")
        drive = open(self.device, 'wb')
        for location in location_map:
            drive.seek(location)
            zeros = bytes(self.sector_size)
            drive.write(zeros)

        # Load the old slack entries, add each entry back to the table if it does not have the id of our removed blob
        current_slack_entries = self.slack_table
        self.slack_table = []
        for entry in current_slack_entries:
            if entry[2] == blob_id:
                continue
            else:
                self.slack_table.append(entry)

        # Save the new slack table
        self.save_slack_table()

        return data_array  # Return our data blob

class file_handler():
    def __init__(self, verbose=False):
        self.v = verbose

    # Set the path to the file or files to hide
    def set_files_to_hide(self, target):
        self.path = target
        self.target_type = None
        self.determine_target_type()
        if self.v: print("Target is a", self.target_type)

    # Use os.path.istype calls to the determine the type of the input that will be used to generate the blob
    #   This information is only used for debuging and inforamtional purposes
    def determine_target_type(self):
        if os.path.isfile(self.path):
            self.target_type = 'file'
        elif os.path.isdir(self.path):
            self.target_type = 'dir'
        else:
            self.target_type = None

    # Make a data blob from the set files
    def make_blob(self):
        # Tar the target file or directory and save the resulting tar.gz file at the blob path
        #   Then load the blob file (tar.gz) from the blob path and return its bytes. Delete the blob file afterwards
        blob_name = 'blob-' + datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + '.tar.gz'
        blob_path = 'staging/'+blob_name
        blob_name.replace(' ', '-')
        print(blob_name)
        # Tar the target and invoke gzip on the tar file, save the file in a temporary location
        subprocess.run(['tar', '-czvf', blob_path, self.path])

        # Read the blob file
        blob = open(blob_path, 'rb').read()
        subprocess.run(['rm', blob_path])  # delete the temporary blob file
        return blob

    # Extract the file or files from a data blob
    def extract_files_from_blob(self, blob, save_path):
        # Save the blob to a temporary file
        blob_path = 'staging/' + 'blob-' + datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + '.tar.gz'
        blob_file = open(blob_path, 'wb')
        blob_file.write(blob)
        # Untar/unzip the file to a provided extraction location
        subprocess.run(['tar', '-xvf', blob_path, '-C', save_path])
        subprocess.run(['rm', blob_path])  # delete the temporary blob file
