# This is the main driver function of the program and handles parsing commandline arguments and setting up the
#  objects used to perform the hiding
import argparse
from file_system_utils import drive_handler, file_handler

if __name__ == '__main__':
    # Create a parser with arguments for the target drive, and the option to input files, extract file to an output
    #   location or simply check the state of the drive. Additionally, provide an option to purge the entire drive.
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', '-t', type=str, help='The path to the mount point of the target drive'
                                                                ' providing the slack space')
    parser.add_argument('--check', '-k', action='store_true', help='If this flag is set, calculate the space on the target but '
                                                         'do not hide or extract files')
    parser.add_argument('--input', '-i', type=str, help='The path to a file to hide in slack space OR'
                                                               ' the path to a directory containing multiple files')
    parser.add_argument('--id', type=int, help='The id to assign the blob hidden in slack space.')
    parser.add_argument('--output', '-o', type=str, help='The path to a directory to place the file or files '
                                                                'extracted from slack space')

    parser.add_argument('--purge', action='store_true', help='Delete all data in slack space on the target device including the'
                                                   ' slack table.')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show verbose output', default=False)
    args = parser.parse_args()

    # Check that a target drive was provided
    if not args.target:
        print("A target drive must be specified")
        quit()

    # Create the drive handler based on the provided target and verbosity level
    d_handler = drive_handler(args.target, verbose=args.verbose)

    # If the check flag was passed, prepare the drive and then exit
    if args.check:
        print("Checking free slack space on:", args.target)
        d_handler.prepare_hidden_storage()
        quit()

    # If the purge flag was passed, purge the drive then exit
    if args.purge:
        print("Purging Slack Space")
        d_handler.prepare_hidden_storage()
        d_handler.purge_slack_space()
        quit()

    # Ensure that only input or output arguments were passed
    if args.input and args.output:
        print("Only one operation can be selected at a time. Either use input to hide files or output to extract them")
        quit()

    # Ensure that at least one option (input/output) was passed
    if args.target and not (args.input or args.output):
        print("Target drive provided with no input or output files")
        quit()

    # If input arguments were passed, prepare the storage and then use a file handler to create a blob that is then
    #   written into slack space. Quit upon completion
    if args.input:
        d_handler.prepare_hidden_storage()

        files = file_handler(verbose=args.verbose)
        files.set_files_to_hide(target=args.input)
        blob = files.make_blob()

        d_handler.write_blob_to_slack(blob, args.id)
        quit()

    # If the output argument was passed, prepare the drive, then read the blob with the associated id from storage,
    #   then use a file handler to extract the file/files from the blob and save them to the desired location.
    #   Quit upon completion
    if args.output:
        d_handler.prepare_hidden_storage()
        blob = d_handler.retrieve_blob_from_slack(blob_id=args.id)

        files = file_handler(verbose=args.verbose)
        files.extract_files_from_blob(blob, args.output)
        quit()